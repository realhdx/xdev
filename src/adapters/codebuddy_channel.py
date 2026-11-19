"""Explicit official CodeBuddy CLI channel; no WorkBuddy Desktop inference."""
from src.adapters import official_channel


def submit(root, identifier, session, steer=False, idle_only=False):
    if steer or idle_only:
        raise ValueError("The official CodeBuddy channel supports explicit queue delivery, not steering")
    result = official_channel.submit(root, identifier, session, client="workbuddy")
    return {**result, "client": "codebuddy-channel", "physical_client": "workbuddy"}
