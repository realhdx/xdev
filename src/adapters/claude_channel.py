"""Explicit official Claude CLI channel; separate from the legacy peer candidate."""
from src.adapters import official_channel


def submit(root, identifier, session, steer=False, idle_only=False):
    if steer or idle_only:
        raise ValueError("The official Claude channel supports explicit queue delivery, not steering")
    result = official_channel.submit(root, identifier, session, client="claude")
    return {**result, "client": "claude-channel", "physical_client": "claude"}
