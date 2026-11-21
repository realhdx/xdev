"""Local pipe protocol for an application's background mail subscriber."""

import json
import threading
from queue import Empty, Full, Queue

from src.adapters.mail_arrival import Subscription


PROTOCOL = "xdev.mail-arrival.v1"


def serve(root, peer, client, session, reader, writer, *, profile=None, ack_timeout=10):
    """Serve one local host registration. Closing its input stops the subscriber."""
    stop = threading.Event()
    replies = Queue(maxsize=1)

    def emit(message):
        writer.write(json.dumps({"protocol": PROTOCOL, **message}, separators=(",", ":")) + "\n")
        writer.flush()

    def receive_acks():
        try:
            while not stop.is_set():
                line = reader.readline(1025)
                if not line or len(line) > 1024 or not line.endswith("\n"):
                    break
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        break
                    replies.put_nowait(item)
                except (ValueError, Full, RecursionError):
                    break
        except (OSError, ValueError):
            pass
        finally:
            stop.set()
            try:
                replies.put_nowait(None)
            except Full:
                pass

    def dispatch(seq, notice_id):
        if stop.is_set():
            return "not_submitted"
        try:
            emit({"event": "mail_available", "inbox_seq": seq, "notice_id": notice_id})
            ack = replies.get(timeout=ack_timeout)
            if (isinstance(ack, dict) and set(ack) == {"protocol", "notice_id", "status"}
                    and ack["protocol"] == PROTOCOL and ack["notice_id"] == notice_id
                    and ack["status"] in ("accepted", "not_submitted", "unknown")):
                return ack["status"]
        except (Empty, OSError, ValueError):
            pass
        stop.set()
        return "unknown"

    sub = Subscription(root, peer=peer, client=client, session=session, profile=profile)
    emit({"event": "ready"})
    worker = threading.Thread(target=receive_acks, name="xdev-mail-host-input", daemon=True)
    worker.start()
    try:
        sub.run(dispatch, stop)
    finally:
        stop.set()
