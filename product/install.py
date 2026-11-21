"""Install or service an unpacked xdev Windows product kit."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType
import zipfile


sys.dont_write_bytecode = True
ROLES = ("laptop", "desktop")
WHEELS = "assets/optional-deps.zip"
ADDRESS_FIELDS = {"device", "primary_name", "legacy_clients", "machine_name", "aliases", "peers", "identity_key"}


def physical(path):
    absolute = Path(path).expanduser().absolute()
    if str(absolute).startswith("\\\\") or ".." in absolute.parts:
        raise ValueError("Use an absolute local installation path without parent traversal")
    for item in (absolute, *absolute.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError(f"Expected a physical local path: {item}")
    return absolute


def load_at(path):
    path = physical(path)
    spec = importlib.util.spec_from_file_location("xdev_kit_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_bytes(path):
    physical(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def device_helpers(kit, metadata):
    member = "src/runtime/device_names.py"
    with (kit / "runtime.zip").open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != metadata["runtime_sha256"]:
            raise ValueError("The checked runtime archive changed")
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            code = compile(archive.read(member), str(kit / "runtime.zip") + "/" + member, "exec")
    names = ModuleType("xdev_release_device_names")
    exec(code, names.__dict__)
    return names


def read_configuration(path, names):
    raw = read_bytes(path)
    config = json.loads(raw.decode("utf-8-sig")) if raw is not None else {}
    if not isinstance(config, dict):
        raise ValueError("Expected config.json to contain a configuration object")
    if "device" in config and config["device"] is None:
        raise ValueError("Invalid null device in the existing configuration")
    names.DeviceDirectory(config)
    return raw, config


def aliases_except(primary, *groups):
    result = []
    seen = {primary.casefold()}
    for group in groups:
        for alias in group:
            folded = alias.casefold()
            if folded not in seen:
                seen.add(folded)
                result.append(alias)
    return result


def configuration(path, previous, options, names):
    config = dict(previous)
    prior = names.DeviceDirectory(previous).check_machine()
    machine = names.machine_name()
    bound = config.get("machine_name")
    old = prior.node if prior.legacy_clients else config.get("device")
    old_role = old if old in ROLES and not {"machine_name", "aliases", "peers"}.intersection(previous) else None
    primary = options.name or (old if old and not old_role and old.casefold() != "local" else machine)
    if not names.valid_name(primary):
        raise ValueError("Invalid primary device name")
    added = [a for a in (old, config.get("device"), options.device) if a and a.casefold() != "local"]
    config.pop("legacy_clients", None)
    config.pop("primary_name", None)
    config.update(device=primary, machine_name=bound or machine,
                  aliases=aliases_except(primary, config.get("aliases", []), added, options.alias))
    established = old and old != "local"
    stable = prior.identity_key("local") if established else primary.casefold()
    config["identity_key"] = options.identity_key or previous.get("identity_key") or stable
    ledger = path.parent / "state/discussion-sends.sqlite3"
    if established and config["identity_key"].casefold() != stable.casefold() and ledger.exists():
        raise ValueError("This mailbox has a group-send ledger; keep its established identity key")
    peers = {key: {**settings, "identity_key": settings.get("identity_key") or prior.identity_key(key)}
             for key, settings in config.get("peers", {}).items()}
    peer = options.peer
    if not peer and (options.peer_alias or options.peer_ssh or options.peer_identity_key):
        raise ValueError("Peer settings require --peer")
    # A historical role-only installation used the opposite name as its SSH host.
    # Retain that route when moving the role to a local alias.
    legacy_peer = ({"laptop": "desktop", "desktop": "laptop"}.get(old_role)
                   if not peers else None)
    if peer or legacy_peer:
        requested = peer or legacy_peer
        if not names.valid_name(requested):
            raise ValueError("Invalid peer device name")
        key = next((name for name in peers if name.casefold() == requested.casefold()), requested)
        if key not in peers and options.peer_identity_key:
            former = next((name for name, item in peers.items()
                           if item["identity_key"].casefold() == options.peer_identity_key.casefold()), None)
            if former:
                if former.casefold() not in {alias.casefold() for alias in options.peer_alias}:
                    raise ValueError("To rename an existing peer, include its previous name as --peer-alias")
                peers[key] = peers.pop(former)
        details = dict(peers.get(key, {}))
        details["ssh"] = options.peer_ssh or details.get("ssh") or legacy_peer or key
        details["aliases"] = aliases_except(key, details.get("aliases", []),
                                            [legacy_peer] if legacy_peer else [], options.peer_alias)
        previous_key = details.get("identity_key")
        if previous_key and options.peer_identity_key and previous_key.casefold() != options.peer_identity_key.casefold() and ledger.exists():
            raise ValueError("This mailbox has a group-send ledger; keep the peer's established identity key")
        details["identity_key"] = options.peer_identity_key or previous_key or legacy_peer or key.casefold()
        peers[key] = details
    config["peers"] = peers
    keep = prior.legacy_clients if options.keep_legacy_clients is None else options.keep_legacy_clients
    if keep:
        role = prior.identity_key("local")
        if not established or role not in ROLES or config["identity_key"].casefold() != role:
            raise ValueError("Keeping legacy clients requires a previously established laptop or desktop identity")
        config.update(primary_name=primary, device=role, legacy_clients=True)
    names.DeviceDirectory(config)
    return config


def project_for_100(settings, names):
    """Keep the compatible role pair readable by both 1.0.0 and already-running role-only clients."""
    directory = names.DeviceDirectory(settings)
    local = directory.mail_device("local")
    result = dict(settings)
    result.pop("primary_name")
    result.pop("legacy_clients")
    result["device"] = local
    result["aliases"] = aliases_except(local, directory.names_for("local"))
    result["peers"] = {}
    for peer, options in settings["peers"].items():
        target = directory.mail_device(peer)
        result["peers"][target] = {**options, "aliases": aliases_except(target, [peer], options.get("aliases", []))}
    names.DeviceDirectory(result)
    return result


def address_fields(settings):
    return {key: value for key, value in settings.items() if key in ADDRESS_FIELDS}


def transition(root, current, target, names, ops, options=None):
    path = root / "config.json"
    before, actual = read_configuration(path, names)
    log_path = root / "state/product/device-compatibility.json"
    old_log = read_bytes(log_path)
    history = json.loads(old_log) if old_log else {"legacy": {}}
    if not isinstance(history, dict) or not isinstance(history.get("legacy"), dict):
        raise ValueError("Invalid legacy device compatibility record")

    def address_format(release):
        if not release:
            return None
        folder = physical(root / "releases" / ops.release_id(release))
        if not folder.is_dir():
            return None
        if not physical(folder / "src/runtime/device_names.py").is_file():
            return "legacy"
        embedded = read_bytes(folder / "src/product.json")
        if embedded and json.loads(embedded).get("version") == "1.0.0":
            return "1.0.0"
        return "current"

    old_style, new_style = address_format(current), address_format(target)
    settings = dict(actual)
    if old_style == "legacy":
        history["legacy"].setdefault(current, actual.get("device"))
        if "product_address" in history:
            settings.update(history["product_address"])
        elif options is not None:
            old_device = actual.get("device")
            if old_device in ROLES and options.device and options.device != old_device:
                raise ValueError("The stated historical role differs from this legacy installation")
            if old_device in (None, "local"):
                stated = options.device or (options.identity_key or "").casefold()
                if stated in ROLES:
                    settings["device"] = stated
                elif (root / "state/discussion-sends.sqlite3").exists():
                    raise ValueError("This unconfigured legacy root has a group-send ledger; supply its historical --device laptop or desktop")
    elif old_style == "1.0.0":
        view = history.get("v100_projection", {}).get(current)
        if view is not None:
            if address_fields(actual) != view["expected"]:
                raise ValueError("Device settings changed under 1.0.0; reconcile them with the saved compatibility record before upgrading")
            settings = {key: value for key, value in actual.items() if key not in ADDRESS_FIELDS}
            settings.update(view["address"])
    if new_style == "legacy":
        if target not in history["legacy"]:
            raise ValueError("This legacy release has no recorded device binding; its address cannot be restored")
        if old_style != "legacy":
            history["product_address"] = address_fields(settings)
        settings = {key: value for key, value in actual.items() if key not in ADDRESS_FIELDS}
        old_device = history["legacy"][target]
        if old_device not in (None, "local", *ROLES):
            raise ValueError("Invalid recorded legacy device binding")
        if old_device is not None:
            settings["device"] = old_device
    elif options is not None:
        settings = configuration(path, settings, options, names)
    if new_style == "1.0.0":
        if settings.get("legacy_clients"):
            projected = project_for_100(settings, names)
            history.setdefault("v100_projection", {})[target] = {
                "address": address_fields(settings), "expected": address_fields(projected)}
            settings = projected
        elif target in history.get("v100_projection", {}):
            del history["v100_projection"][target]
    names.DeviceDirectory(settings)
    after = before if settings == actual else (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    new_log = ops.canonical_json(history) if old_log or history["legacy"] or history.get("v100_projection") else None
    return ((log_path, old_log, new_log), (path, before, after))


def releases(deploy, path):
    raw = read_bytes(path)
    if raw is None:
        return raw, []
    catalog = json.loads(raw)
    if not isinstance(catalog, dict) or catalog.get("schema") != 1 or not isinstance(catalog.get("entries"), list):
        raise ValueError("Invalid installed xdev product catalog")
    seen = set()
    for entry in catalog["entries"]:
        if (not isinstance(entry, dict) or entry.get("product") != "xdev"
                or any(not isinstance(entry.get(k), str) or not entry[k] for k in ("series", "version"))):
            raise ValueError("Invalid installed product release")
        deploy.release_id(entry.get("runtime_sha256"))
        key = (entry["series"], entry["version"])
        if key in seen:
            raise ValueError("Duplicate product version in the installed catalog")
        seen.add(key)
    return raw, catalog["entries"]


def record(entries, new):
    label = (new["series"], new["version"])
    updated = []
    for entry in entries:
        if (entry["series"], entry["version"]) == label:
            if entry["runtime_sha256"] != new["runtime_sha256"]:
                raise ValueError("This series/version already names a different installed runtime")
        else:
            updated.append(entry)
    return updated + [new]


def update_file(deploy, path, before, after):
    if read_bytes(path) != before:
        raise RuntimeError(f"The local {path.name} changed concurrently")
    if after == before:
        return
    if after is None:
        path.unlink()
    else:
        deploy.write_atomic(path, after)


def runtime_status(deploy, root, *, verify):
    if not root.exists() or (root.is_dir() and not any(root.iterdir())):
        return {"root": str(root), "active": None, "previous": None, "releases": [], "active_verified": False}
    return deploy.status(root, verify)


def status(deploy, root, catalog_path, names):
    result = runtime_status(deploy, root, verify=True)
    _, entries = releases(deploy, catalog_path)
    by_digest = {entry["runtime_sha256"]: entry for entry in entries}
    for version in (result["active"], result["previous"]):
        if version and version not in by_digest:
            raw = read_bytes(root / "releases" / version / "src/product.json")
            if raw is not None:
                identity = json.loads(raw)
                if (not isinstance(identity, dict) or identity.get("product") != "xdev"
                        or not all(isinstance(identity.get(k), str) and identity[k] for k in ("series", "version"))):
                    raise ValueError("Invalid embedded product identity")
                by_digest[version] = {**identity, "runtime_sha256": version}
    _, config = read_configuration(root / "config.json", names)
    deps = physical(root / "deps")
    receipt = read_bytes(deps / ".xdev-deps.json")
    if receipt:
        mcp = {"state": "receipt_present", "archive_sha256": json.loads(receipt)["archive_sha256"]}
    else:
        mcp = {"state": "unmanaged" if deps.exists() else "absent"}
    directory = names.DeviceDirectory(config)
    device, key = directory.node, directory.identity_key("local")
    mode = "configured" if config.get("device") else "machine_name"
    if (result["active"] and config.get("device") in (None, "local") and
            not physical(root / "releases" / result["active"] / "src/runtime/device_names.py").exists()):
        device, key, mode = config.get("device"), None, "legacy_runtime_default"
    return {**result, "device": device, "machine_name": directory.machine_name, "address_mode": mode, "identity_key": key,
            "aliases": config.get("aliases", []), "peers": config.get("peers", {}), "legacy_clients": directory.legacy_clients,
            "active_product": by_digest.get(result["active"]),
            "previous_product": by_digest.get(result["previous"]), "mcp_dependencies": mcp,
            "entrypoint": str(root / "xdev.cmd")}


def service(kit, options):
    if os.name != "nt" or sys.version_info[:2] != (3, 14):
        raise RuntimeError("Use Windows Python 3.14 to run this product kit")
    kit = physical(kit)
    metadata = load_at(kit / "verify.py").verify_directory(kit)
    names = device_helpers(kit, metadata)
    deploy = load_at(kit / "deploy" / "deploy.py")
    root = physical(options.root or deploy.install_root())
    with_dsh = getattr(options, "with_dsh", False)
    if with_dsh and (root != physical(deploy.install_root()) or getattr(options, "cli_only", False)):
        raise ValueError("The DSH client requires the standard installation root and MCP dependencies")
    if kit.is_relative_to(root) or root.is_relative_to(kit):
        raise ValueError("Keep the installation root separate from the extracted kit")
    catalog_dir = physical(root / "state" / "product")
    catalog_path = catalog_dir / "releases.json"
    current = runtime_status(deploy, root, verify=False)["active"]
    if options.command == "status":
        return status(deploy, root, catalog_path, names)

    expected = getattr(options, "expect_current", None)
    if expected is not None and current != (None if expected == "none" else deploy.release_id(expected)):
        raise RuntimeError("Active release differs from --expect-current")
    rollback = options.command == "rollback"
    if options.command in ("upgrade", "rollback") and not current:
        raise ValueError(f"{options.command} requires an active installation")
    if not current and getattr(options, "keep_legacy_clients", False):
        raise ValueError("--keep-legacy-clients applies to an existing installation with historical role clients")
    new_release = {k: metadata[k] for k in ("product", "series", "version", "runtime_sha256")}
    if not rollback:
        transition(root, current, metadata["runtime_sha256"], names, deploy, options)
        # Reject a conflicting name before allocating space for another candidate.
        _, known = releases(deploy, catalog_path)
        record(known, new_release)
        # Stage is idempotent and leaves the active runtime untouched.
        deploy.stage(root, kit / "runtime.zip", metadata["runtime_sha256"])
    catalog_dir.mkdir(exist_ok=True)
    with deploy.deployment_lock(catalog_dir):
        active, previous = deploy.read_pointer(root)
        if active != current:
            raise RuntimeError("The active release changed; inspect status before retrying")
        old_catalog, entries = releases(deploy, catalog_path)
        installed_mcp = None
        if rollback:
            selected = options.release or previous
            if options.version is not None:
                candidates = [e for e in entries if e["series"] == metadata["series"] and e["version"] == options.version]
                if not candidates:
                    raise ValueError("The selected product version is not registered on this installation")
                selected = candidates[0]["runtime_sha256"]
                entries = record(entries, candidates[0])
        else:
            selected = metadata["runtime_sha256"]
            entries = record(entries, new_release)
            if not options.cli_only:
                optional = load_at(kit / "deploy" / "optional.py")
                manifest = json.loads((kit / "package-manifest.json").read_bytes())
                installed_mcp = optional.install(root, kit / WHEELS, manifest["files"][WHEELS]["sha256"])
        new_catalog = deploy.canonical_json({"schema": 1, "entries": entries})
        if rollback and not entries:
            new_catalog = old_catalog
        if not selected:
            raise ValueError("No previous release recorded")
        binding_files = transition(root, current, selected, names, deploy, None if rollback else options)
        client_files, client_receipt = [], None
        if root == physical(deploy.install_root()) and (with_dsh or (current and not getattr(options, "cli_only", False))):
            sys.path.insert(0, str(kit / "deploy"))
            try:
                import dsh_roots
                client_files, client_receipt = dsh_roots.profile_transition(root, current, selected,
                                                                           explicit=with_dsh, rollback=rollback)
            finally:
                sys.path.pop(0)
        changes = []
        try:
            for path, before, after in ((catalog_path, old_catalog, new_catalog), *binding_files, *client_files):
                if before != after:
                    if path.name == "cordis.patch.yml":
                        dsh_roots.clients.publish_profile(path, before, after)
                    else:
                        update_file(deploy, path, before, after)
                    changes.append((path, after, before))
            deploy.change_pointer(root, selected, expect=current or "none", rollback=rollback)
        except Exception:
            for path, before, after in reversed(changes):
                if path.name == "cordis.patch.yml":
                    dsh_roots.clients.publish_profile(path, before, after)
                else:
                    update_file(deploy, path, before, after)
            raise
        result = status(deploy, root, catalog_path, names)
        if installed_mcp:
            result["mcp_dependencies"] = {"state": "verified", "reused": installed_mcp["reused"],
                                          "archive_sha256": installed_mcp["archive_sha256"],
                                          "preserved_external_directories": installed_mcp["preserved_external_directories"]}
        if client_receipt:
            result["dsh"] = client_receipt
        return result


def parser():
    interface = argparse.ArgumentParser(description=__doc__)
    interface.add_argument("--root", type=Path, help="Isolated local root; default is the current user's standard location")
    commands = interface.add_subparsers(dest="command", required=True)
    for name in ("install", "upgrade"):
        action = commands.add_parser(name)
        action.add_argument("--name", help="Choose this device's address; the default is its Windows computer name")
        action.add_argument("--with-dsh", action="store_true", help="Enable two-way original-chat mail in the installed DeepSeek Harness profile")
        action.add_argument("--alias", action="append", default=[], help="Additional address for this device (repeatable)")
        action.add_argument("--identity-key", help="Initial device address to keep when adopting an existing mailbox")
        action.add_argument("--peer", help="Name of an authorized remote device")
        action.add_argument("--peer-ssh", help="Existing OpenSSH host or alias to reach the peer")
        action.add_argument("--peer-alias", action="append", default=[], help="Additional address for this peer (repeatable)")
        action.add_argument("--peer-identity-key", help="Peer's original address when it was renamed")
        action.add_argument("--device", choices=ROLES, help="Compatibility spelling for a local --alias")
        action.add_argument("--keep-legacy-clients", action=argparse.BooleanOptionalAction, default=None,
                            help="Preserve live role-only clients while using canonical names in new tools")
        action.add_argument("--cli-only", action="store_true", help="Skip the offline MCP dependency installation")
        action.add_argument("--expect-current", metavar="SHA256|none")
    commands.add_parser("status")
    rollback = commands.add_parser("rollback")
    select = rollback.add_mutually_exclusive_group()
    select.add_argument("--version", help="An already registered version in this kit's series")
    select.add_argument("--release", help="SHA-256 of a staged runtime, including legacy releases")
    rollback.add_argument("--expect-current", metavar="SHA256")
    return interface


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        output = service(Path(__file__).absolute().parent, parser().parse_args())
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps({"ok": True, **output}, ensure_ascii=False))
