"""Authenticated localhost delivery to a cooperating application's resident event loop."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import struct
import threading

LIMIT = 16384
TIMEOUT = 5
ROUTE_KEYS = {"port", "identity_directory"}


def _physical(value):
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts or str(path).startswith(("//", "\\\\")) or
            any(p.is_symlink() or (p.exists() and getattr(p.lstat(), "st_file_attributes", 0) & 0x400)
                for p in (path, *path.parents))):
        raise ValueError("Use a physical, absolute local identity directory")
    return path


def validate(config):
    if not isinstance(config, dict) or set(config) != ROUTE_KEYS:
        raise ValueError("A resident route requires port and identity_directory")
    if type(config["port"]) is not int or not 1 <= config["port"] <= 65535:
        raise ValueError("Supply the registered local TCP port")
    if not isinstance(config["identity_directory"], str):
        raise ValueError("Supply a local identity directory")
    _physical(config["identity_directory"])


def provision(directory):
    """Generate a private per-application CA and two scoped endpoint identities once."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    directory = _physical(directory)
    # Work in a new, private directory. Never reuse partially written credentials.
    directory.mkdir(mode=0o700)
    if os.name == "nt":
        from src.adapters.codex_receiver import _private
        _private(directory, directory=True)
    now = datetime.now(timezone.utc)
    pem = serialization.Encoding.PEM
    authority = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "xdev resident local CA")])

    def certificate(name, public_key):
        return (x509.CertificateBuilder().subject_name(name).issuer_name(issuer).public_key(public_key)
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=365)))

    ca_cert = (certificate(issuer, authority.public_key())
               .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
               .sign(authority, hashes.SHA256()))
    (directory / "ca.pem").write_bytes(ca_cert.public_bytes(pem))
    for role, usage in (("host", ExtendedKeyUsageOID.SERVER_AUTH), ("caller", ExtendedKeyUsageOID.CLIENT_AUTH)):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "xdev resident " + role)])
        cert = (certificate(name, key.public_key()).add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .add_extension(x509.ExtendedKeyUsage([usage]), False))
        if role == "host":
            cert = cert.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        (directory / (role + ".pem")).write_bytes(cert.sign(authority, hashes.SHA256()).public_bytes(pem))
        key_path = directory / (role + ".key")
        with os.fdopen(os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
            stream.write(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def _context(directory, server):
    path = _physical(directory)
    role = "host" if server else "caller"
    ca, cert, key = [_physical(path / name) for name in ("ca.pem", role + ".pem", role + ".key")]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cafile=str(ca))
    ctx.load_cert_chain(str(cert), str(key))
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _frame(sock, value):
    body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(body) <= LIMIT:
        raise ValueError("Resident message exceeds the frame limit")
    sock.sendall(struct.pack("!I", len(body)) + body)


def _read(sock):
    def exactly(size):
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise OSError("Incomplete resident frame")
            data.extend(chunk)
        return data

    length, = struct.unpack("!I", exactly(4))
    if not 0 < length <= LIMIT:
        raise ValueError("Invalid resident frame length")
    value = json.loads(exactly(length))
    if not isinstance(value, dict):
        raise ValueError("Expected a resident object")
    return value


def exchange(config, notice):
    """Connection refused is definite non-submission. A failed TLS/ACK is never replayed."""
    validate(config)
    ctx = _context(config["identity_directory"], False)
    try:
        raw = socket.create_connection(("127.0.0.1", config["port"]), timeout=TIMEOUT)
    except ConnectionRefusedError:
        return {"status": "unavailable_before_submit"}
    with raw, ctx.wrap_socket(raw, server_hostname="localhost") as link:
        _frame(link, notice)
        ack = _read(link)
        if any(ack.get(k) != notice[k] for k in ("protocol", "client", "session", "message_id", "notice_id")):
            raise ValueError("Resident acknowledgement identifies a different notice")
        if ack.get("status") not in {"accepted", "requires_activation", "unknown"}:
            raise ValueError("Invalid resident acknowledgement")
        return ack


@contextmanager
def listen(directory, client, dispatch, *, port=0):
    """Listen in the application's own process. Dispatch must confirm the original chat ID and placement."""
    from src.adapters import sessions
    sessions.client_name(client)
    tls = _context(directory, True)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(TIMEOUT)
            try:
                with tls.wrap_socket(self.request, server_side=True) as link:
                    notice = _read(link)
                    if (notice.get("protocol") != "xdev.receiver.v1" or notice.get("client") != client
                            or notice.get("mode") not in {"send_now", "queue"}
                            or any(not isinstance(notice.get(k), str) or not notice[k] for k in
                                   ("message_id", "notice_id", "session"))):
                        return
                    session = sessions.canonical_session(client, notice["session"])
                    ack = {k: notice[k] for k in ("protocol", "client", "session", "message_id", "notice_id")}
                    result = dispatch(session, notice)
                    if result is None:
                        ack["status"] = "requires_activation"
                    elif (isinstance(result, dict) and result.get("session") == session
                          and result.get("placement") in {"current_turn", "original_session", "next_turn"}):
                        ack.update(status="accepted", placement=result["placement"])
                    else:
                        ack["status"] = "unknown"
                    _frame(link, ack)
            except (OSError, ValueError, TypeError):
                return

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

        def __init__(self, *args):
            self.slots = threading.BoundedSemaphore(16)
            super().__init__(*args)

        def process_request(self, request, address):
            if not self.slots.acquire(blocking=False):
                self.close_request(request)
                return
            try:
                super().process_request(request, address)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                self.slots.release()

        def handle_error(self, request, client_address):
            # Application exceptions may follow submission. Close without an ACK.
            pass

    with Server(("127.0.0.1", port), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        try:
            yield {"port": server.server_address[1], "identity_directory": str(_physical(directory))}
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Provision an identity for a cooperating local host.")
    parser.add_argument("directory", type=Path, help="New private identity directory; parent must exist")
    args = parser.parse_args()
    provision(args.directory.absolute())
    print(json.dumps({"ok": True, "identity_directory": str(args.directory.absolute())}))
