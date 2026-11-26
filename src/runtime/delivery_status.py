"""Resolve one letter's delivery using only evidence scoped to its original ID."""

def summarize(submission, *, report=None, observation=None, linked_reply=None, addressed=True):
    identifier = submission.get("id")
    status = submission.get("status")
    facts = set()
    observed = observation or {}
    if (identifier and observed.get("id") == identifier and observed.get("ok") is True
            and observed.get("scope") == "recipient_mailbox"):
        if observed.get("received_at"):
            facts.add("recipient_mailbox")
        if observed.get("read_at") and observed.get("read_scope") == "session":
            facts.add("recipient_read")
        if observed.get("read_at") and observed.get("read_scope") == "device":
            facts.add("device_read")
        if observed.get("withdrawn_at"):
            facts.add("recipient_withdrawal")
    if identifier and linked_reply and linked_reply.get("reply_to") == identifier:
        facts.add("linked_reply")
    placement = (submission.get("delivery_receipt") or {}).get("scope")
    placements = {placement}
    if identifier and report and report.get("message_id") == identifier and report.get("inbox_status") == "received":
        facts.add("carrier_report")
        placements.add(report.get("placement"))
        if report.get("session_status") == "recalled":
            facts.add("recipient_withdrawal")
    if submission.get("inbox_status") in {"received", "duplicate"}:
        facts.add("inbox_receipt")
    if (placements & {"original_session", "current_turn"} or submission.get("current_turn_accepted")
            or submission.get("session_accepted") is True and "next_turn" not in placements
            and submission.get("session_status") != "queued_not_current"):
        facts.add("session_accepted")
    if "next_turn" in placements or submission.get("session_status") == "queued_not_current":
        facts.add("session_queued")
    if status in {"queue_acknowledged", "broker_acknowledged"}:
        facts.add("carrier_ack")
    if status == "no_route_available" or status == "not_submitted":
        facts.add("not_submitted")
    if ((status == "inbox_failed" and submission.get("inbox_status") == "failed")
            or (submission.get("inbox_status") == "not_attempted" and submission.get("ok") is False)):
        facts.add("delivery_rejected")

    if "recipient_withdrawal" in facts:
        state = "withdrawn"
    elif "linked_reply" in facts:
        state = "replied"
    elif "recipient_read" in facts:
        state = "read"
    elif "session_accepted" in facts:
        state = "accepted"
    elif "session_queued" in facts:
        state = "queued"
    elif facts & {"inbox_receipt", "carrier_report", "recipient_mailbox", "device_read"}:
        state = "pending_wake" if addressed else "mailbox_received"
    elif "carrier_ack" in facts:
        state = "submitted"
    elif facts & {"delivery_rejected", "not_submitted"}:
        state = "failed"
    else:
        state = "unknown"
    retry = "check_same_id"
    if state in {"read", "replied", "withdrawn", "mailbox_received"}:
        retry = "never"
    elif state == "failed" and "not_submitted" in facts:
        retry = "retry_same_id"
    return {"state": state, "retry": retry, "evidence": sorted(facts)}
