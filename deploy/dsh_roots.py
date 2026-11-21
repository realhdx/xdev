"""Bind the installed DSH receiver to existing original chats in this user's profile."""

import argparse
from compression import zstd
from datetime import UTC, datetime
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

import clients


def restore_managed(before, after, live):
    """Undo only the changes we published, against a possibly edited profile."""
    if live == after:
        return before
    if live == before:
        return live
    source, target, current = [data.splitlines(keepends=True) for data in (after, before, live)]
    unchanged = {}
    for i, j, count in SequenceMatcher(None, source, current, autojunk=False).get_matching_blocks():
        unchanged.update((i + n, j + n) for n in range(count))
    operations = SequenceMatcher(None, source, target, autojunk=False).get_opcodes()
    for kind, a, b, c, d in reversed(operations):
        if kind == 'equal':
            continue
        if a == b:
            left = unchanged.get(a - 1, -1) + 1
            right = unchanged.get(a, len(current))
            if left != right:
                raise ValueError("DSH profile changed at a managed insertion; original profile backup is available")
            current[left:left] = target[c:d]
        else:
            left = unchanged.get(a)
            if left is None or any(unchanged.get(i) != left + i - a for i in range(a, b)):
                raise ValueError("A managed DSH setting was edited since deployment; original profile backup is available")
            current[left:left + b - a] = target[c:d]
    return b''.join(current)


def profile_transition(root, current_release, target_release, *, explicit, rollback):
    if rollback and current_release == target_release:
        return [], None
    path, _ = clients.client_config("dsh")
    if not path.is_file() and not explicit:
        return [], None
    before = clients.client_bytes("dsh", path) if path.exists() or explicit else b""
    entry = clients.expected_entry(root)
    selected = any(clients.dsh_block(entry, nl).encode() in before for nl in ("\r\n", "\n"))
    disabled = re.search(rb"(?m)^- id: xdev[ \t]*\r?\n(?:[ ]{2}[^\n]*\n)*?  disabled: true[ \t]*\r?$", before)
    if not explicit and (not selected or disabled):
        return [], None
    history = root / "state/product/dsh-profile"
    history.mkdir(exist_ok=True, parents=True)
    def snapshot(data):
        key = clients.sha(data)
        record = history / (key + ".bin")
        if not record.exists():
            with record.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        if clients.read_config(record) != data:
            raise ValueError("DSH rollback snapshot could not be verified")
        return key
    edge = history / ((target_release if rollback else current_release or 'initial') + '--' +
                      ((current_release or 'initial') if rollback else target_release) + '.json')
    edge_before = clients.read_config(edge) if edge.exists() else None
    if rollback and edge_before:
        saved = json.loads(edge_before, object_pairs_hook=clients.strict_object)
        values = []
        for name in ('before', 'after'):
            digest = saved[name]
            if not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise ValueError("Invalid DSH profile snapshot")
            image = clients.read_config(history / (digest + '.bin'))
            if clients.sha(image) != digest:
                raise ValueError("DSH profile snapshot checksum mismatch")
            values.append(image)
        after = restore_managed(*values, before)
    elif rollback:
        # A rollback issued with this kit can point to a release from before
        # profile management. Leave a compatible baseline alone, but never
        # silently keep a helper pinned to the departing release.
        if current_release and (str(root / 'releases' / current_release).encode() in before.replace(b'\\\\', b'\\') or
                                b'    - id: xdev-dsh-active-' in before):
            raise ValueError("No original DSH transition snapshot for these two versions; roll back through the recorded intermediate release")
        return [], None
    else:
        if disabled:
            raise ValueError("This DSH profile explicitly disabled xdev")
        data, _, _ = clients.transform(before, 'yaml-patch', entry)
        after = clients.original_dsh_profile(data, root, prepare=True, release=target_release)
    changes = [(path, before if path.exists() else None, after)]
    if not rollback and current_release != target_release and (edge_before is None or before != after):
        record = {'before': snapshot(before), 'after': snapshot(after)}
        changes.insert(0, (edge, edge_before, json.dumps(record, sort_keys=True).encode()))
    return changes, {"profile_configured": True, "changed": before != after,
                     "profile_sha256": clients.sha(after), "transition": str(edge) if current_release != target_release else None}


def automatic_receiver(data, root, adapter, *, prepare=False):
    """Single managed host plugin; newly opened chats need no list or file edits."""
    digest = hashlib.sha256(adapter.HOST_PLUGIN.encode()).hexdigest()[:16]
    install = root / "integrations" / ("dsh-active-" + digest)
    bridge = root / "state/dsh/active-roots"
    for path in (install, bridge):
        if any(node.is_symlink() or node.is_junction() for node in (path, *path.parents)):
            raise ValueError("Use a physical local DSH receiver directory")
    text = data.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"
    cfg = {"bridgeDirectory": str(bridge), "profile": "active-roots", "sessions": [],
           "discovery": "active_original", "aclPython": sys.executable}
    block = nl.join(["- insert:", "    - id: xdev-dsh-active-" + digest,
                     "      name: " + json.dumps(str(install / "index.js")),
                     "      config: " + json.dumps(cfg), ""])
    pattern = (r'(?m)^- insert:\r?\n    - id: xdev-dsh-active-([0-9a-f]{16})\r?\n'
               r'      name: ([^\r\n]+)\r?\n      config: ([^\r\n]+)\r?\n')
    matches = list(re.finditer(pattern, text))
    if len(matches) > 1 or text.count('- id: xdev-dsh-active-') != len(matches):
        raise ValueError("Conflicting or locally overridden automatic receiver; review DSH profile")
    if matches:
        old = matches[0]
        if (Path(json.loads(old[2])) != root / "integrations" / ("dsh-active-" + old[1]) / "index.js" or
                json.loads(old[3], object_pairs_hook=clients.strict_object) != cfg):
            raise ValueError("Locally modified DSH automatic receiver")
        updated = text[:old.start()] + block + text[old.end():]
    else:
        updated = text + ("" if text.endswith(nl) else nl) + block
    if prepare:
        if not install.exists():
            install.parent.mkdir(exist_ok=True, parents=True)
            staging = install.with_name(install.name + ".stage-" + uuid4().hex)
            adapter.write_plugin_bundle(staging, bridge, [], profile=bridge.name, automatic=True)
            if os.name == "nt":
                utility = Path(os.environ["SystemRoot"]) / "System32/icacls.exe"
                subprocess.run([str(utility), str(staging), "/grant", "*" + adapter._windows_user_sid() + ":(OI)(CI)RX"],
                               check=True, timeout=12, capture_output=True)
            try:
                staging.rename(install)
            except OSError:
                if not install.is_dir():
                    raise
        if (install / "index.js").read_text(encoding="utf-8") != adapter.HOST_PLUGIN:
            raise ValueError("Host receiver differs from this release")
    return (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + updated.encode("utf-8")


def original_sessions(directory, canonical, selected=None):
    wanted = {canonical(value) for value in selected} if selected else None
    result = set()
    patterns = (f"*/{value}/session.v4.jsonl.zstd" for value in sorted(wanted)) if wanted else ["*/*/session.v4.jsonl.zstd"]
    for path in (p for pattern in patterns for p in directory.glob(pattern)):
        if not path.parent.name.startswith("session-"):
            continue
        if any(node.is_symlink() or node.is_junction() for node in (directory, path.parent.parent, path.parent, path)):
            continue
        with zstd.open(path, "rt", encoding="utf-8") as stream:
            header = json.loads(stream.readline(131072))
        if (header.get("type") == "session" and header.get("version") == 4
                and header.get("id") == path.parent.name and header.get("origin") != "subagent"
                and not header.get("parentSession") and header.get("delegationDepth", 0) == 0):
            identifier = canonical(header["id"])
            if identifier in result:
                raise ValueError(f"Ambiguous DSH original session: {identifier}")
            result.add(identifier)
    if wanted and wanted != result:
        raise ValueError("No original chat found for: " + ", ".join(sorted(wanted - result)))
    return result


def planned_patch(data, original, install, name, bridge, python, *, scoped=False):
    text = data.decode("utf-8-sig")
    entries = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not entries or entries[0] != "- insert:" or any(line in {"---", "..."} for line in entries):
        raise ValueError("Unrecognized DSH user patch")
    # Edit only our own single-line JSON config generated by xdev. Other
    # receiver bindings retain their prior session set, host and ledger.
    existing = set()
    overrides = {}
    for update in re.finditer(r"(?m)^- id: (xdev-dsh-receiver[^\s]*)[ \t]*\r?\n((?:[ \t]+[^\n]*\n?)*)", text):
        flag = re.search(r"(?m)^  disabled: (true|false)[ \t]*(?:#.*)?\r?$", update[2])
        if flag:
            overrides[update[1]] = flag[1] == "true"
    block_pattern = (r'(?m)^    - id: (xdev-dsh-receiver[^\s]*)\r?\n'
                     r'      name: ([^\r\n]+)\r?\n      config: ([^\r\n]+)(?=\r?$)')
    own = None
    for match in re.finditer(block_pattern, text):
        config = json.loads(match[3])
        if match[1] == name:
            if own or Path(json.loads(match[2])) != install / "index.js":
                raise ValueError("Conflicting DSH receiver entry")
            own = match
        elif not overrides.get(match[1], False):
            existing.update(config["sessions"])
    if own and overrides.get(name):
        raise ValueError("The listed receiver is explicitly disabled in this profile")
    if not own and re.search(rf"(?m)^\s*- id: {re.escape(name)}\s*$", text):
        raise ValueError("Existing DSH receiver has a different config format")
    target = sorted(original - existing)
    if not target:
        return data, target
    cfg = {"bridgeDirectory": str(bridge), "profile": bridge.name, "sessions": target,
           "aclPython": python, "xdevBundleAccessRevision": 1}
    wire = json.dumps(cfg, ensure_ascii=False)
    if own:
        old = json.loads(own[3])
        if old == cfg:
            return data, target
        if (set(old) - set(cfg) or any(old.get(k) != cfg[k] for k in
                                       ("bridgeDirectory", "profile", "aclPython"))):
            raise ValueError("Existing DSH receiver points to a different binding")
        # Keep explicit older bindings if a chat was archived during discovery.
        if scoped:
            cfg = dict(old)
        cfg["sessions"] = sorted(set(old["sessions"]) | set(target))
        wire = json.dumps(cfg, ensure_ascii=False)
        if old == cfg:
            return data, cfg["sessions"]
        new = text[:own.start(3)] + wire + text[own.end(3):]
        return ((b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"")
                + new.encode("utf-8")), cfg["sessions"]
    nl = "\r\n" if "\r\n" in text else "\n"
    block = nl.join(["", "- insert:", "    - id: " + name,
                     "      name: " + json.dumps(str(install / "index.js")),
                     "      config: " + wire, ""])
    return data + block.encode("utf-8"), target


def _session_tool_config(data, root, release, python, enable, nl):
    """Only replace the exact generated plug-in block, keeping all other YAML byte-identical."""
    marker = b"    - id: xdev-session-mailbox"
    text = data.decode("utf-8")
    pattern = (r'(?m)^- insert:\r?\n    - id: xdev-session-mailbox(?:-[0-9a-f]{12})?\r?\n'
               r'      name: ([^\r\n]+)\r?\n      config: ([^\r\n]+)\r?\n')
    matches = list(re.finditer(pattern, text))
    if data.count(marker) != len(matches) or len(matches) > 1:
        raise ValueError("Conflicting DSH session tool configuration")
    if matches:
        match = matches[0]
        prior = Path(json.loads(match[1]))
        prior_config = json.loads(match[2], object_pairs_hook=clients.strict_object)
        if (prior.parent.name != "adapters" or prior.name != "dsh_host_tool.mjs" or
                prior.parent.parent.name != "src" or not re.fullmatch("[a-f0-9]{64}", prior.parents[2].name) or
                prior.parents[2].parent != root / "releases" or
                prior_config != {"python": python, "helper": str(prior.with_suffix(".py"))}):
            raise ValueError("The session tool configuration was locally modified")
    plugin = root / "releases" / release / "src/adapters/dsh_host_tool.mjs"
    helper = plugin.with_suffix(".py")
    if enable and not all(p.is_file() and p.resolve() == p for p in (plugin, helper)):
        raise ValueError("The active release does not include physical DSH session tool files")
    # Cordis reconciles an existing ID with fiber.update(config), which can
    # leave its old JS tool definition alive while the Python helper advances.
    # Replacing the entry identity makes it import the new release module.
    block = nl.join(["- insert:", "    - id: xdev-session-mailbox-" + release[:12],
                     "      name: " + json.dumps(str(plugin)),
                     "      config: " + json.dumps({"python": python, "helper": str(helper)}), ""]) if enable else ""
    if matches:
        text = text[:match.start()] + block + text[match.end():]
    elif block:
        text += ("" if text.endswith(nl) else nl) + block
    return text.encode("utf-8")


def mailbox_plugin(before, root, release, session_tool):
    entry = clients.expected_entry(root)
    blocks = [(nl, clients.dsh_block(entry, nl).encode()) for nl in ("\r\n", "\n")]
    matches = [(nl, block) for nl, block in blocks for _ in range(before.count(block))]
    if len(matches) != 1:
        raise ValueError("Expected one exact xdev stdio plugin")
    nl, prefix = matches[0]
    offset = before.index(prefix) + len(prefix)
    tail = before[offset:]
    position, env_line = 0, None
    for line in tail.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.strip() and not stripped.startswith(b"#"):
            if not line.startswith(b"        "):
                break
            if re.match(rb"""^        ["']?(?:serverName|transport|command|args)["']?\s*:""", line):
                raise ValueError("Duplicate generated xdev stdio setting in DSH profile")
            if re.match(rb"""^        ["']?env["']?\s*:""", line):
                if env_line is not None or not line.startswith(b"        env: {"):
                    raise ValueError("Existing xdev env needs a lossless YAML edit")
                env_line = (position, line)
        position += len(line)
    env = {}
    end = offset
    if env_line:
        index, original = env_line
        env = json.loads(original.strip()[5:], object_pairs_hook=clients.strict_object)
        if not isinstance(env, dict):
            raise ValueError("Unsupported existing plugin environment")
        offset += index
        end = offset + len(original)
    env["XDEV_RELOAD_GENERATION"] = release
    if session_tool:
        env["XDEV_DSH_SESSION_TOOL"] = "1"
    else:
        env.pop("XDEV_DSH_SESSION_TOOL", None)
    after = before[:offset] + ("        env: " + json.dumps(env, separators=(",", ":")) + nl).encode() + before[end:]
    return _session_tool_config(after, root, release, entry["command"], session_tool, nl)


def refresh_mcp(session_tool=None):
    """Trigger native hot reload of this profile's xdev stdio and original-caller tool."""
    root = Path(os.environ["LOCALAPPDATA"]) / "CrossDeviceCollab"
    release = (root / "current.txt").read_text(encoding="ascii").splitlines()[0]
    if not re.fullmatch("[0-9a-f]{64}", release):
        raise ValueError("Invalid installed release")
    if session_tool is None:
        session_tool = (root / "releases" / release / "src/adapters/dsh_host_tool.mjs").is_file()
    patch = Path.home() / ".dsh/cordis.patch.yml"
    before = clients.read_config(patch)
    after = mailbox_plugin(before, root, release, session_tool)
    if after == before:
        return {"release": release, "changed": False, "session_tool": session_tool}
    backup = patch.with_name(patch.name + ".bak-xdev-mcp-" + uuid4().hex)
    with backup.open("xb") as stream:
        stream.write(before)
    if backup.read_bytes() != before:
        raise RuntimeError("Backup verification failed")
    clients.replace_cas(patch, after, clients.sha(before))
    return {"release": release, "changed": True, "backup": str(backup), "session_tool": session_tool,
            "readback_verified": clients.read_config(patch) == after,
            "before_sha256": clients.sha(before), "after_sha256": clients.sha(after)}


def run(apply, selected=None, *, all_existing=False):
    root = Path(os.environ["LOCALAPPDATA"]) / "CrossDeviceCollab"
    release = (root / "current.txt").read_text(encoding="ascii").splitlines()[0]
    if not re.fullmatch(r"[a-f0-9]{64}", release):
        raise ValueError("Invalid installed release pointer")
    sys.path.insert(0, str(root / "releases" / release))
    from src.adapters import dsh

    home = Path.home() / ".dsh"
    patch = home / "cordis.patch.yml"
    before = clients.read_config(patch)
    known = original_sessions(home / "sessions", dsh.canonical_session, selected)
    install = root / "integrations" / "dsh-receiver-listed-v2"
    bridge = root / "state" / "dsh" / "listed-roots"
    for path in [install, *install.parents, bridge, *bridge.parents]:
        if path.is_symlink() or path.is_junction():
            raise ValueError("Use physical local plugin paths")
    after, target = planned_patch(before, known, install, "xdev-dsh-receiver-listed",
                                  bridge, sys.executable, scoped=bool(selected))
    answer = {"root_sessions_found": len(known), "sessions": sorted(known) if selected else target, "applied": False,
              "before_sha256": clients.sha(before), "after_sha256": clients.sha(after)}
    if not apply:
        return answer
    if not target:
        answer.update(applied=True, changed=False, readback_verified=clients.read_config(patch) == before)
        return answer
    installed_before = install.exists()
    if installed_before:
        plugin = (install / "index.js").read_text(encoding="utf-8")
        previous_static = "0b116bf1610f85bca6cc92f0f5c716c1f29bcd9502e012bf82b0697a749d459e"
        if ((plugin != dsh.HOST_PLUGIN and hashlib.sha256(plugin.encode()).hexdigest() != previous_static) or
                json.loads((install / "package.json").read_text())["name"] != "@local/xdev-dsh-receiver-v2"):
            raise ValueError("Unexpected receiver bundle at installation path")
        if after == before:
            answer.update(applied=True, changed=False, installed=str(install), readback_verified=True)
            return answer
    else:
        dsh.write_plugin_bundle(install, bridge, target, profile=bridge.name)
    if os.name == "nt" and not (selected and installed_before):
        # Windows gives a mode-0700 directory created over elevated SSH to
        # OWNER RIGHTS (Administrators). The desktop runs as the ordinary user.
        sid = dsh._windows_user_sid()
        utility = Path(os.environ["SystemRoot"]) / "System32/icacls.exe"
        subprocess.run([str(utility), str(install), "/grant", "*" + sid + ":(OI)(CI)RX"],
                       check=True, timeout=12, capture_output=True)
    if before != after:
        backup = patch.with_name(patch.name + ".bak-xdev-roots-" + uuid4().hex)
        with backup.open("xb") as stream:
            stream.write(before)
        if backup.read_bytes() != before:
            raise RuntimeError("DSH user config backup verification failed")
        clients.replace_cas(patch, after, clients.sha(before))
        answer["backup"] = str(backup)
    answer.update(applied=True, changed=before != after, installed=str(install),
                  readback_verified=clients.read_config(patch) == after,
                  verified_at=datetime.now(UTC).isoformat())
    return answer


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    op = parser.add_mutually_exclusive_group()
    op.add_argument("--apply", action="store_true")
    op.add_argument("--refresh-mcp", action="store_true")
    op.add_argument("--restore-chat-tool", action="store_true",
                    help="restore the original shared stdio mailbox tool for this DSH profile")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--session", action="append", metavar="ID", help="bind only this existing original chat (repeatable)")
    scope.add_argument("--all-existing", action="store_true", help="explicitly include every existing original chat in this profile")
    args = parser.parse_args()
    if (args.refresh_mcp or args.restore_chat_tool) and (args.session or args.all_existing):
        parser.error("--session and --all-existing apply to receiver configuration only")
    result = refresh_mcp(False) if args.restore_chat_tool else (refresh_mcp() if args.refresh_mcp else run(args.apply, args.session, all_existing=args.all_existing))
    print(json.dumps(result, ensure_ascii=False))
