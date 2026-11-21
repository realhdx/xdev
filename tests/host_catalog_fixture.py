"""Two independently named native MCP interfaces for the discovery integration test."""

import json
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

kind, state, reply = sys.argv[1:]
app = FastMCP("Alpha Desk" if kind == "alpha" else "Beta Desk")
ro = ToolAnnotations(readOnlyHint=True)


def write(op, data):
    with Path(state).open("a", encoding="utf8") as out:
        out.write(json.dumps([op, data]) + "\n")


if kind == "alpha":

    @app.tool(annotations=ro)
    def lookup(chat: str) -> dict[str, Any]:
        write("read", chat)
        return {"chat": chat, "original": chat == "group/42", "boot": "host-a"}

    @app.tool()
    def interrupt(chat: str, payload: str, correlation: str) -> dict[str, Any]:
        write("send", [chat, payload, correlation])
        return {
            "chat": chat if reply != "wrong" else "not-original",
            "correlation": correlation,
            "received": True,
            "where": "current_turn",
        }
else:

    @app.tool(
        annotations=ro,
        meta={
            "io.xdev/receiver": {
                "version": 1,
                "client": "previously-unknown",
                "role": "resolve",
                "inputs": {"session": "room"},
                "outputs": {"session": "roomId", "original": "valid", "instance": "process"},
            }
        },
    )
    def describe_room(room: str) -> dict[str, Any]:
        write("read", room)
        return {"roomId": room, "valid": room == "group/42", "process": "host-b"}

    if reply != "read-only":

        @app.tool(
            meta={
                "io.xdev/receiver": {
                    "version": 1,
                    "client": "previously-unknown",
                    "role": "submit",
                    "inputs": {"session": "dest", "text": "prompt", "notice_id": "key"},
                    "outputs": {"session": "target", "notice_id": "key", "accepted": "placed", "placement": "scope"},
                    "placements": ["next_turn"],
                }
            }
        )
        def enqueue_to_room(dest: str, prompt: str, key: str) -> dict[str, Any]:
            write("send", [dest, prompt, key])
            return {"target": dest, "key": key, "placed": True, "scope": "next_turn"}


app.run(transport="stdio")
