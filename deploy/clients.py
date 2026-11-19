"""Register installed xdev in existing client files, with byte backups and CAS undo.

No client or model is launched by apply/inspect/restore. verify only launches the
registered MCP server through the official SDK and calls the read-only who tool.
"""

import argparse
import asyncio
import ctypes
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import site
import sys
import tempfile
import time
from uuid import uuid4


PRODUCTS = {
    "workbuddy": (".workbuddy/mcp.json", "json"),
    "antigravity": (".gemini/config/mcp_config.json", "json"),
    "lmstudio": (".lmstudio/mcp.json", "json"),
    "dsh": (".dsh/cordis.patch.yml", "yaml-patch"),
    "copilot": (".copilot/mcp-config.json", "json"),
    "codebuddy": (".codebuddy/.mcp.json", "json"),
    "gemini": (".gemini/settings.json", "json"),
    "kimi-code": (".kimi-code/mcp.json", "json"),
    "vscode": ("AppData/Roaming/Code/User/mcp.json", "json-servers"),
    "cursor": (".cursor/mcp.json", "json"),
}
NEW_SECTION_CLIENTS = {"copilot", "codebuddy", "gemini", "kimi-code", "vscode", "cursor"}


def client_config(product):
    relative, kind = PRODUCTS[product]
    home = Path.home()
    path = home / relative
    locations = {"copilot": ("COPILOT_HOME", "mcp-config.json"),
                 "kimi-code": ("KIMI_CODE_HOME", "mcp.json"),
                 "vscode": ("APPDATA", "Code/User/mcp.json")}
    if product in locations:
        variable, suffix = locations[product]
        value = os.environ.get(variable)
        if value:
            if not Path(value).is_absolute():
                raise ValueError(f"{variable} must be absolute")
            path = Path(value) / suffix
    elif product == "codebuddy":
        # The client reads the first existing user file in this order.
        candidates = (path, home / ".codebuddy/mcp.json", home / ".codebuddy.json")
        path = next((candidate for candidate in candidates if candidate.exists()), path)
    return path, kind


def sha(data):
    return hashlib.sha256(data).hexdigest()


def strict_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("Duplicate JSON key; refusing a lossy rewrite")
        value[key] = item
    return value


def expected_entry(root, product=None):
    python = Path(sys.executable).resolve()
    if os.name == "nt" and python.name.lower() != "python.exe":
        raise ValueError("Use the native python.exe to register the stdio entry")
    if not (root / "launch.py").is_file():
        raise ValueError("The stable installed launcher is missing")
    entry = {"command": str(python),
             "args": ["-I", "-B", "-X", "utf8", str(root / "launch.py"), "mcp"]}
    if product in {"copilot", "codebuddy", "vscode", "cursor"}:
        entry["type"] = "stdio"
    if product == "copilot":
        entry["tools"] = ["xdev_who", "xdev_help", "xdev_file", "xdev_exec", "xdev_job", "xdev_msg"]
    return entry


def dsh_block(entry, newline="\n"):
    lines = ["- insert:", "    - id: xdev",
             "      name: '@deepseek-ai/dsh-mcp-client'", "      config:",
             "        serverName: xdev", "        transport: stdio",
             "        command: " + json.dumps(entry["command"], ensure_ascii=False),
             "        args: " + json.dumps(entry["args"], ensure_ascii=False)]
    return newline.join(lines) + newline


def read_config(path):
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ValueError("Expected an existing physical client configuration file")
    data = path.read_bytes()
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("Client configuration is unexpectedly large")
    return data


def transform(data, kind, entry, *, allow_new_section=False):
    text = data.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    if kind in {"json", "json-servers"}:
        section = "servers" if kind == "json-servers" else "mcpServers"
        try:
            value = json.loads(text, object_pairs_hook=strict_object)
        except json.JSONDecodeError as error:
            raise ValueError("Expected strict JSON. For JSONC, use the client's native MCP settings.") from error
        if not isinstance(value, dict):
            raise ValueError("Expected a client configuration object")
        created_section = section not in value and allow_new_section
        if created_section:
            value[section] = {}
        if not isinstance(value.get(section), dict):
            raise ValueError(f"Expected the client's existing {section} object")
        servers = value[section]
        if "xdev" in servers:
            if servers["xdev"] != entry:
                raise ValueError("An unlike xdev entry already exists; review before replacing")
            return data, list(servers), False
        old = json.loads(text, object_pairs_hook=strict_object)
        servers["xdev"] = entry
        indent = "\t" if re.search(r"(?m)^\t+\"", text) else 2
        result = json.dumps(value, ensure_ascii=False, indent=indent, allow_nan=False)
        encoded = bom + (result.replace("\n", newline) + newline).encode("utf-8")
        check = json.loads(encoded.decode("utf-8-sig"), object_pairs_hook=strict_object)
        del check[section]["xdev"]
        if created_section:
            del check[section]
        if check != old:
            raise ValueError("Other client settings did not survive serialization")
        return encoded, list(servers), True
    if kind != "yaml-patch":
        raise ValueError("Unknown client format")
    effective = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not effective or effective[0] != "- insert:" or any(line in {"---", "..."} for line in effective):
        raise ValueError("Expected the installed DSH single-document insert patch list")
    block = dsh_block(entry, newline)
    if re.search(r"(?m)^\s+(?:-\s+)?(?:id|serverName):\s*['\"]?xdev['\"]?\s*$", text):
        if not text.endswith(block):
            raise ValueError("An unlike xdev DSH plugin already exists")
        return data, re.findall(r"(?m)^\s+- id:\s*(\S+)", text), False
    result = data + (b"" if data.endswith(b"\n") else newline.encode()) + newline.encode() + block.encode("utf-8")
    return result, [*re.findall(r"(?m)^\s+- id:\s*(\S+)", text), "xdev"], True


def replace_windows(path, temporary):
    replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                        ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
    replace.restype = ctypes.c_int
    if not replace(str(path), str(temporary), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def replace_cas(path, data, expected):
    """Last-moment digest check and atomic replacement; keep Windows file metadata."""
    if sha(read_config(path)) != expected:
        raise ValueError("Client configuration changed; compare-and-swap refused")
    temporary = path.with_name(path.name + ".xdev-stage-" + uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for delay in (0.05, 0.1, 0.2, 0.4, 0.8, None):
            if sha(read_config(path)) != expected:
                raise ValueError("Client configuration changed while staging; refused")
            try:
                if os.name == "nt":
                    replace_windows(path, temporary)
                else:
                    os.replace(temporary, path)
                break
            except OSError as error:
                # Windows retains both names on ERROR_UNABLE_TO_REMOVE_REPLACED.
                if delay is None or getattr(error, "winerror", None) not in {32, 33, 1175}:
                    raise
                time.sleep(delay)
        if sha(read_config(path)) != sha(data):
            raise ValueError("Client configuration changed after publication; inspect the backup")
    finally:
        temporary.unlink(missing_ok=True)


def inspect(product, path, kind, root):
    data = read_config(path)
    entry = expected_entry(root, product)
    _, names, changed = transform(data, kind, entry, allow_new_section=product in NEW_SECTION_CLIENTS)
    return {"product": product, "path": str(path), "format": kind,
            "sha256": sha(data), "server_names": names if not changed else names[:-1],
            "xdev_exact": not changed, "expected_entry": entry}


def apply(product, path, kind, root, expected):
    data = read_config(path)
    if sha(data) != expected:
        raise ValueError("Client configuration differs from the reviewed SHA-256")
    entry = expected_entry(root, product)
    updated, names, changed = transform(data, kind, entry, allow_new_section=product in NEW_SECTION_CLIENTS)
    if not changed:
        return {**inspect(product, path, kind, root), "changed": False}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:10]
    backup = path.with_name(path.name + ".bak-xdev-" + stamp)
    with backup.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if sha(backup.read_bytes()) != expected:
        raise ValueError("Original byte backup could not be verified")
    replace_cas(path, updated, expected)
    receipt = {"schema": 1, "product": product, "path": str(path), "format": kind,
               "time_utc": datetime.now(UTC).isoformat(), "changed": True,
               "before_sha256": expected, "after_sha256": sha(updated),
               "backup_path": str(backup), "backup_sha256": sha(data),
               "server_names": names, "xdev_entry": entry,
               "other_semantics_preserved": True, "host_client_reload": "not_requested_or_verified"}
    directory = root / "state" / "client-rollouts"
    directory.mkdir(exist_ok=True)
    receipt_path = directory / (stamp + "-" + product + ".json")
    receipt["receipt_path"] = str(receipt_path)
    with receipt_path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return receipt


def restore(receipt_path):
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("schema") != 1 or receipt.get("changed") is not True:
        raise ValueError("Expected a changed client rollout receipt")
    path = Path(receipt["path"])
    backup = Path(receipt["backup_path"])
    if backup.parent != path.parent or not backup.name.startswith(path.name + ".bak-xdev-"):
        raise ValueError("The backup is outside the client configuration directory")
    data = read_config(backup)
    if sha(data) != receipt["before_sha256"] or sha(data) != receipt["backup_sha256"]:
        raise ValueError("The original backup checksum does not match")
    replace_cas(path, data, receipt["after_sha256"])
    return {"restored": True, "product": receipt["product"], "path": str(path), "sha256": sha(data)}


async def verify(product, path, kind, root, peer):
    before = inspect(product, path, kind, root)
    if not before["xdev_exact"]:
        raise ValueError("The actual client file does not contain the expected xdev registration")
    entry = before["expected_entry"]
    dependencies = str(root / "deps")
    sys.path.insert(0, dependencies)
    site.addsitedir(dependencies)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=entry["command"], args=entry["args"], cwd=tempfile.gettempdir())
    report = {"product": product, "path": str(path), "config_sha256": before["sha256"],
              "method": "official_sdk_exact_registered_entry", "host_client_reload": "not_requested_or_verified"}
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        async with asyncio.timeout(60):
            async with stdio_client(params, errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialization = await session.initialize()
                    tools = await session.list_tools()
                    names = sorted(tool.name for tool in tools.tools)
                    if len(names) != 6 or "xdev_who" not in names:
                        raise ValueError("Expected the installed six-tool MCP catalog")
                    report.update(protocol=initialization.protocolVersion, tools=names)
                    report["identities"] = []
                    for host in ["local", peer]:
                        reply = await session.call_tool("xdev_who", {"host": host})
                        value = reply.structuredContent or json.loads(reply.content[0].text)
                        if reply.isError or value.get("ok") is not True:
                            raise ValueError("Read-only xdev_who did not succeed")
                        report["identities"].append({"requested": host, **value})
    if sha(read_config(path)) != before["sha256"]:
        raise ValueError("Client configuration changed during verification")
    report["server_closed"] = True
    report["ok"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "apply", "verify", "restore"])
    parser.add_argument("--product", choices=PRODUCTS)
    parser.add_argument("--expect-sha256")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--peer", help="Registered xdev peer device name or alias")
    args = parser.parse_args()
    if args.action == "restore":
        if args.receipt is None:
            parser.error("restore requires --receipt")
        result = restore(args.receipt)
    else:
        if not args.product:
            parser.error("a product is required")
        path, kind = client_config(args.product)
        root = Path(os.environ["LOCALAPPDATA"]) / "CrossDeviceCollab"
        if args.action == "inspect":
            result = inspect(args.product, path, kind, root)
        elif args.action == "apply":
            if not args.expect_sha256 or not re.fullmatch(r"[a-f0-9]{64}", args.expect_sha256):
                parser.error("apply requires the reviewed --expect-sha256")
            result = apply(args.product, path, kind, root, args.expect_sha256)
        else:
            if not args.peer:
                parser.error("verify requires --peer")
            result = asyncio.run(verify(args.product, path, kind, root, args.peer))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
