"""Check the two packaged mailbox implementations over a local test pipe."""

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def worker(runtime, launcher, args):
    sys.path.insert(0, runtime)
    from src.runtime import transport

    def exchange(ssh_args, wire, timeout):
        print(json.dumps({"test_frame": base64.b64encode(wire).decode("ascii")}), flush=True)
        return json.loads(sys.stdin.readline())

    transport._json_request = exchange
    if launcher:
        sys.argv = [launcher, *args]
        runpy.run_path(launcher, run_name="__main__")
    else:
        from src.cli.main import main
        raise SystemExit(main(args))


def verify(windows_zip, linux_tar, distro):
    def wsl(*args):
        return ["wsl.exe", "-d", distro, "--", *map(str, args)]

    def linux_path(path):
        drive, tail = path.resolve().as_posix().split(":/", 1)
        return f"/mnt/{drive.lower()}/{tail}"

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CODEX_", "CLAUDE_", "DSH_", "OPENCODE_", "XDEV_"))}
    with tempfile.TemporaryDirectory(prefix="xdev-pair-check-") as directory:
        work = Path(directory)
        win_runtime = work / "windows-runtime"
        assert hashlib.sha256(windows_zip.read_bytes()).hexdigest() == (
            "72441a653f88466e2d57741c2e2a0d687acdd96be2e2def30d10de859e9bf91c")
        with zipfile.ZipFile(windows_zip) as outer:
            runtime_name, = [n for n in outer.namelist() if n.endswith("runtime.zip")]
            with zipfile.ZipFile(io.BytesIO(outer.read(runtime_name))) as runtime:
                runtime.extractall(win_runtime)

        expected = Path(str(linux_tar) + ".sha256").read_text().split()[0]
        assert hashlib.sha256(linux_tar.read_bytes()).hexdigest() == expected
        with tarfile.open(linux_tar) as bundle:
            assert all(m.isfile() for m in bundle.getmembers())
            bundle.extractall(work, filter="data")
        linux_app, = work.glob("xdev-linux-mail-*/xdev")
        machine = {}
        for node, peer in [("alpha", "beta"), ("beta", "alpha")]:
            state = work / node
            state.mkdir()
            config = {"device": node, "peers": {peer: {
                "rpc": "linux-mailbox", "ssh": "unused.invalid", "mail_transport": "direct"}}}
            (state / "config.json").write_text(json.dumps(config), encoding="utf-8")
            if node == "alpha":
                machine[node] = (
                    [sys.executable, "-B", __file__, "worker", str(win_runtime), ""],
                    [sys.executable, "-B", "-m", "src.cli.main"],
                    {**env, "XDEV_HOME": str(state)}, win_runtime)
            else:
                prefix = ["env", "XDEV_HOME="+linux_path(state), "python3", "-B"]
                machine[node] = (
                    wsl(*prefix, linux_path(Path(__file__)), "worker",
                        linux_path(linux_app.parent / "runtime"), linux_path(linux_app)),
                    wsl(*prefix, linux_path(linux_app)), env, work)

        def call(node, *args):
            peer = "beta" if node == "alpha" else "alpha"
            sender, _, local_env, cwd = machine[node]
            _, receiver, peer_env, peer_cwd = machine[peer]
            with subprocess.Popen([*sender, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  encoding="utf-8", env=local_env, cwd=cwd) as process:
                for line in process.stdout:
                    result = json.loads(line)
                    if "test_frame" not in result:
                        break
                    remote = subprocess.run([*receiver, "_rpc_mail", node],
                        input=base64.b64decode(result["test_frame"]) + b"\n",
                        stdout=subprocess.PIPE, cwd=peer_cwd, env=peer_env, timeout=30)
                    response = json.loads(remote.stdout)
                    process.stdin.write(json.dumps(response) + "\n")
                    process.stdin.flush()
                assert process.wait(timeout=20) == 0 and result["ok"], result
                return result

        for source, target in [("alpha", "beta"), ("beta", "alpha")]:
            first = "question-" + source
            reply = "reply-" + target
            call(source, "msg", "send", "--as-device", "--to", target, "--id", first,
                 "--subject", "Rows", "--text", "Count rows", "--wait-reply", "0",
                 "--transport", "direct")
            assert call(target, "msg", "read", first)["message"]["text"] == "Count rows"
            call(target, "msg", "reply", first, "--id", reply, "--text", "12 rows",
                 "--transport", "direct")
            received = call(source, "msg", "read", reply)["message"]
            assert (received["reply_to"], received["kind"], received["text"]) == (first, "reply", "12 rows")
            print(f"{source} -> {target} -> {source}: linked reply read")


if __name__ == "__main__":
    if sys.argv[1:2] == ["worker"]:
        worker(sys.argv[2], sys.argv[3], sys.argv[4:])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("windows_zip", type=Path)
        parser.add_argument("linux_tar", type=Path)
        parser.add_argument("--distro", default="Ubuntu")
        opts = parser.parse_args()
        verify(opts.windows_zip.resolve(), opts.linux_tar.resolve(), opts.distro)
