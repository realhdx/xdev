"""Carrier privacy and real boto3 HTTP acceptance against an optional local SQS emulator."""

import base64
from contextlib import ExitStack, closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.adapters import caller_context, inprocess_cli
from src.runtime import managed_queue as queue, session_relay as mail


def write_site(root, own, peer, url, other, endpoint, key):
    receiver = [
        sys.executable, "-c",
        "import json,sys;from pathlib import Path; n=json.load(sys.stdin);"
        "p=Path('accepted-'+n['notice_id']);p.touch(exist_ok=False);"
        "print(json.dumps({**{k:n[k] for k in ('protocol','client','session','message_id','notice_id')},"
        "'status':'accepted','placement':'current_turn'}))"]
    root.mkdir()
    (root/"config.json").write_text(json.dumps({
        "device": own, "peers": {peer: {"mail_transport": "sqs"}},
        "session_hosts": {"writer": {"argv": receiver}},
        "managed_queue": {"region": "us-east-1", "queue_url": url, "endpoint_url": endpoint,
                          "peers": {peer: {"key_file": str(key), "queue_url": other,
                                           "receiver_apps": ["writer"]}}}}))


class QueueSecurityTests(unittest.TestCase):
    def test_endpoint_validation(self):
        for url in ("http://external.example/123456789012/in.fifo", "https://usr@x.example/123456789012/in.fifo",
                    "https://x.example/123456789012/in", "https://x.example/123456789012/in.fifo?x=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                queue._url(url, queue=True)

    def test_unsafe_resource_policy_fails_closed(self):
        from unittest.mock import Mock
        url = "https://sqs.us-east-1.amazonaws.com/123456789012/alpha.fifo"
        attrs = {"MaximumMessageSize":"1048576", "MessageRetentionPeriod":"86400", "FifoQueue":"true",
                 "QueueArn":"arn:aws:sqs:us-east-1:123456789012:alpha.fifo"}
        policy = lambda principal: json.dumps({"Statement":[{"Effect":"Allow","Principal":principal}]})
        for change in ({"MessageRetentionPeriod":"86401"},{"RedrivePolicy":"{}"},{"FifoQueue":"false"},
                       {"Policy":policy("*")},{"Policy":policy({"AWS":"arn:aws:iam::000000000000:role/other"})}):
            with self.subTest(change=change), patch.object(queue, "_call", return_value={"Attributes":{**attrs,**change}}):
                with self.assertRaises(ValueError):
                    queue._attributes(Mock(), url)


@unittest.skipUnless(importlib.util.find_spec("moto") and importlib.util.find_spec("boto3"),
                     "optional SQS SDK and local test emulator required")
class SQSHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from moto.server import ThreadedMotoServer
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        environment = {"AWS_ACCESS_KEY_ID": "fixture", "AWS_SECRET_ACCESS_KEY": "fixture",
                       "AWS_SESSION_TOKEN": "fixture", "AWS_EC2_METADATA_DISABLED": "true",
                       "AWS_DEFAULT_REGION": "us-east-1", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        cls.stack.enter_context(patch.dict(os.environ, environment))
        cls.server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
        cls.server.start()
        cls.stack.callback(cls.server.stop)
        host, port = cls.server.get_host_and_port()
        cls.endpoint = f"http://{host}:{port}"

    def setUp(self):
        import boto3
        self.api = self.enterContext(closing(boto3.client("sqs", endpoint_url=self.endpoint, region_name="us-east-1")))
        names = ("alpha", "beta")
        self.urls = {name: self.api.create_queue(QueueName=f"{name}-{uuid4().hex[:8]}.fifo", Attributes={
            "FifoQueue":"true", "MessageRetentionPeriod":"3600", "MaximumMessageSize":"1048576"})["QueueUrl"] for name in names}
        temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        key = temp/"pair.key"
        key.write_text(os.urandom(32).hex())
        self.roots = {name: temp/name for name in names}
        for own, other in (names, names[::-1]):
            write_site(self.roots[own], own, other, self.urls[own], self.urls[other], self.endpoint, key)

    def run_cli(self, site, argv, *, session="room"):
        with caller_context.bind({"xdev/session":{"client":"writer","session":session}}):
            return inprocess_cli.invoke(["msg",*argv], root=self.roots[site])

    def inspect(self, site):
        return self.api.receive_message(QueueUrl=self.urls[site], VisibilityTimeout=0,
               MaxNumberOfMessages=1, MessageSystemAttributeNames=["All"]).get("Messages", [])

    @unittest.skipUnless(importlib.util.find_spec("nats"), "optional nats-py required for real failover")
    def test_unreachable_real_jetstream_connection_switches_once_to_private_queue(self):
        import socket
        source = self.roots["alpha"]
        path = source / "config.json"
        config = json.loads(path.read_text())
        config["peers"]["beta"] = {"mail_routes": ["relay", "sqs"]}
        with socket.socket() as closed_port:
            closed_port.bind(("127.0.0.1", 0))
            user_pass = source / "qa-password"
            user_pass.write_text("test-only")
            config["session_relay"] = {"url": f"nats://127.0.0.1:{closed_port.getsockname()[1]}",
                                       "user": "alpha", "password_file": str(user_pass), "peers": {
                                           "beta": {"key_file": config["managed_queue"]["peers"]["beta"]["key_file"],
                                                    "encryption": "required", "receiver_apps": ["writer"]}}}
            path.write_text(json.dumps(config))
            args = ["send", "--to", "beta", "--client", "writer", "--session", "room",
                    "--id", "two-carriers", "--text", "one letter", "--delivery-report"]
            sent = self.run_cli("alpha", args)
            self.assertTrue(sent["ok"], sent)
            self.assertEqual([(item["transport"], item["status"]) for item in sent["route_attempts"]],
                             [("relay", "not_submitted"), ("sqs", "queue_acknowledged")])
            self.assertTrue(self.run_cli("alpha", args)["route_reused"])
        for node in ("beta", "alpha"):
            got = self.run_cli(node, ["listen", "--transport", "sqs", "--limit", "1", "--timeout", "2"])
            self.assertEqual(len(got["results"]), 1)
        report = self.run_cli("alpha", ["status", "two-carriers"])
        self.assertEqual((report["transport"], report["report"]["placement"]), ("sqs", "current_turn"))
        self.assertEqual(len(list(self.roots["beta"].glob("accepted-*"))), 1)

    def test_cli_roundtrip_and_provider_observations(self):
        mid = uuid4().hex
        first, second = uuid4().hex, uuid4().hex
        sent = self.run_cli("alpha", ["send","--to","beta","--client","writer","--session",second,"--id",mid,
                                      "--subject","private subject","--text","private body with challenge "+mid,
                                      "--delivery-report"], session=first)
        self.assertTrue(sent["ok"], sent)
        self.assertEqual(sent["status"],"queue_acknowledged")
        raw = self.inspect("beta")[0]
        wire = base64.b64decode(raw["Body"], validate=True)
        self.assertTrue(wire.startswith(mail.SEALED_PREFIX))
        exposed = wire + json.dumps(raw.get("Attributes",{})).encode()
        for private in (mid, first, second, "private subject", "private body", "writer"):
            self.assertNotIn(private.encode(), exposed)
        item = queue.listen(self.roots["beta"], limit=1, timeout=3)["results"][0]
        self.assertEqual(item["session_notification"]["delivery_receipt"]["scope"], "current_turn")
        self.assertEqual(self.inspect("beta"), [])
        read = self.run_cli("beta", ["read",mid], session=second)
        self.assertEqual(read["message"]["text"],"private body with challenge "+mid)
        self.assertEqual(queue.listen(self.roots["alpha"],limit=1,timeout=3)["results"][0]["session_notification"]["status"],
                         "not_requested")
        self.assertEqual(self.run_cli("alpha",["status",mid], session=first)["report"]["placement"],"current_turn")
        returned = self.run_cli("beta",["reply",mid,"--text","verified "+mid], session=second)
        self.assertTrue(returned["ok"], returned)
        self.assertEqual(queue.listen(self.roots["alpha"],limit=1,timeout=3)["results"][0]["session_notification"]["delivery_receipt"]["scope"],"current_turn")
        answer = self.run_cli("alpha",["read",returned["id"]], session=first)
        self.assertEqual((answer["message"]["text"],answer["message"]["reply_to"]),("verified "+mid,mid))
        self.assertEqual(self.inspect("alpha"), [])
        self.assertEqual([len(list(root.glob("accepted-*"))) for root in self.roots.values()], [1,1])

    def test_modified_payload_is_removed_without_host_action(self):
        alpha, beta = queue.Settings(self.roots["alpha"]), queue.Settings(self.roots["beta"])
        token = alpha.group("alpha","beta",self.urls["beta"])
        target = {"device":"beta","client":"writer","session":"room"}
        letter = {"id":"private-id","envelope_version":2,"from":"alpha","to":"beta","subject":"private",
                  "text":"private content","recipient_identity":target}
        _, wrong = mail._packet(alpha,letter,None,context=b"different carrier or queue")
        for payload in (wrong, b'{"subject":"plaintext"}'):
            self.api.send_message(QueueUrl=self.urls["beta"],MessageBody=base64.b64encode(payload).decode("ascii"),
                                  MessageGroupId=token, MessageDeduplicationId=hashlib.sha256(payload).hexdigest())
            item = queue.listen(self.roots["beta"],limit=1,timeout=3)["results"][0]
            self.assertEqual(item["status"],"rejected")
        self.assertEqual(self.inspect("beta"), [])
        self.assertFalse((self.roots["beta"]/"state").exists())

    def test_report_failure_preserves_source_without_second_host_call(self):
        mid = uuid4().hex
        sent = self.run_cli("alpha", ["send", "--to", "beta", "--client", "writer", "--session", "room",
                                      "--id", mid, "--subject", "retry", "--text", "retry", "--delivery-report"])
        self.assertTrue(sent["ok"], sent)
        received = []
        call = queue._call

        def intercept(api, operation, **kwargs):
            if operation == "send_message":
                raise queue.Unavailable("synthetic return-path outage")
            response = call(api, operation, **kwargs)
            if operation == "receive_message":
                received.extend(response.get("Messages", []))
            return response

        with patch.object(queue, "_call", side_effect=intercept), self.assertRaises(queue.Unavailable):
            queue.listen(self.roots["beta"], limit=1, timeout=3)
        self.assertEqual(len(received), 1)
        self.assertEqual(len(list(self.roots["beta"].glob("accepted-*"))), 1)
        # End the ordinary visibility interval early on the fixture queue.
        self.api.change_message_visibility(QueueUrl=self.urls["beta"], ReceiptHandle=received[0]["ReceiptHandle"],
                                           VisibilityTimeout=0)
        retried = queue.listen(self.roots["beta"], limit=1, timeout=3)["results"][0]
        self.assertEqual(retried["id"], mid)
        self.assertIn("report_id", retried)
        returned = queue.listen(self.roots["alpha"], limit=1, timeout=3)["results"][0]
        self.assertEqual(returned["id"], retried["report_id"])
        self.assertEqual(self.run_cli("alpha", ["status", mid])["report"]["placement"], "current_turn")
        self.assertEqual(len(list(self.roots["beta"].glob("accepted-*"))), 1)
        self.assertEqual(self.inspect("beta"), [])
