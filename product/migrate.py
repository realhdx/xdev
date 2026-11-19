"""Move a device's mailbox and group-send receipts to a new installation."""

import argparse
from contextlib import closing, ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import zipfile


KIT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(KIT))


DATABASES = {
    "messages.sqlite3": {"messages": {"seq", "id", "sender", "recipient", "text", "reply_to", "received_at", "read_at"}},
    "discussion-sends.sqlite3": {"plans": {"author", "operation", "fingerprint"},
                                 "accepted": {"author", "operation", "letter", "receipt"}},
}
FORMAT = "xdev.windows-pair.mailbox.1"


def connection(path, mode):
    return closing(sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=10))


def check_database(path, name):
    with connection(path, "ro") as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError(f"Database integrity check failed: {name}")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, required in DATABASES[name].items():
            columns = {row[1] for row in db.execute("SELECT * FROM pragma_table_info(?)", (table,))}
            if table not in tables or not required <= columns:
                raise ValueError(f"Unexpected database layout: {name}")


def source_identity(config, directory, names, stated):
    if stated is not None and not names.valid_name(stated):
        raise ValueError("Invalid source device name")
    if not stated and config.get("device") in (None, "local"):
        raise ValueError("For an unconfigured legacy installation, give its historical address with --device")
    if stated and not directory.same(stated, directory.node):
        if config.get("device") not in (None, "local"):
            raise ValueError("The stated source does not match this installation")
        # Unconfigured historical installations sometimes used a role on the wire.
        return stated, list(directory.names_for("local")), stated.casefold()
    return (directory.node, [name for name in directory.names_for("local") if name != directory.node],
            directory.identity_key("local"))


def export_snapshot(root, archive, device, ops, *, aliases=(), identity_key=None):
    from install import aliases_except, physical
    if archive.exists():
        raise ValueError("Choose a new backup filename")
    address = {"device": device, "identity_key": identity_key or device.casefold(),
               "aliases": [device, *aliases_except(device, aliases)]}
    state = physical(root / "state")
    sources = {name: physical(state / name) for name in DATABASES}
    sources = {name: path for name, path in sources.items() if path.is_file()}
    if not sources:
        raise ValueError("This installation has no mailbox or group-send history to export")
    with tempfile.TemporaryDirectory(dir=archive.parent) as tmp:
        scratch = Path(tmp)
        with ExitStack() as locks:
            for path in sources.values():
                writer = locks.enter_context(connection(path, "rw"))
                writer.execute("BEGIN IMMEDIATE")
            if any(physical(state / name).exists() for name in DATABASES.keys() - sources.keys()):
                raise ValueError("A database was created during export; retry to include it")
            for name, path in sources.items():
                with connection(path, "ro") as source, closing(sqlite3.connect(scratch / name)) as dest:
                    source.backup(dest)
                    dest.execute("PRAGMA journal_mode=DELETE")
                    dest.execute("VACUUM")
        files = {}
        bundle = scratch / "snapshot.zip"
        with zipfile.ZipFile(bundle, "x") as target:
            for name in sources:
                path = scratch / name
                check_database(path, name)
                files[name] = {"bytes": path.stat().st_size, "sha256": ops.digest(path)}
                target.write(path, name)
            target.writestr("manifest.json", ops.canonical_json({"format": FORMAT, **address, "files": files}))
        checksum = ops.digest(bundle)
        os.rename(bundle, archive)  # Windows rename refuses to replace an existing file.
    return {"archive": str(archive), **address, "sha256": checksum, "databases": sorted(files)}


def restore_snapshot(root, archive, directory, checksum, ops, naming, *, source_device=None):
    from install import physical
    state = physical(root / "state")
    journal = physical(state / "product" / "mail-import.json")
    with archive.open("rb") as stream, ExitStack() as stack:
        if hashlib.file_digest(stream, "sha256").hexdigest() != ops.release_id(checksum):
            raise ValueError("The backup checksum differs from the export receipt")
        stream.seek(0)
        bundle = stack.enter_context(zipfile.ZipFile(stream))
        stack.enter_context(ops.deployment_lock(root))
        tmp = stack.enter_context(tempfile.TemporaryDirectory(dir=state))
        scratch = Path(tmp)
        entries = bundle.infolist()
        names = {entry.filename for entry in entries}
        if (len(entries) != len(names) or "manifest.json" not in names
                or not names - {"manifest.json"} or not names <= {*DATABASES, "manifest.json"}
                or bundle.getinfo("manifest.json").file_size > 8192
                or any(entry.compress_type != zipfile.ZIP_STORED for entry in entries)):
            raise ValueError("Unexpected mailbox snapshot members")
        manifest = json.loads(bundle.read("manifest.json"))
        files = manifest["files"]
        if manifest["format"] != FORMAT or set(files) != names - {"manifest.json"}:
            raise ValueError("The snapshot format or database list does not match")
        source = manifest["device"]
        aliases = manifest.get("aliases", [])
        if not naming.valid_name(source) or not isinstance(aliases, list) or not all(naming.valid_name(a) for a in aliases):
            raise ValueError("Invalid backup device identity")
        if source_device is not None and source.casefold() != source_device.casefold():
            raise ValueError("--source-device differs from the device in the backup")
        if source.casefold() != directory.node.casefold():
            if source_device is None or not directory.same(source, directory.node):
                raise ValueError(f"To adopt {source!r}, register it as a local --alias and confirm --source-device on restore")
        # Original snapshots contained a legacy role in the device field and carried no separate key.
        identity = manifest.get("identity_key", source.casefold())
        if not naming.valid_name(identity) or identity.casefold() != directory.identity_key("local"):
            raise ValueError(f"Set the fresh installation's --identity-key and --alias to the source key {identity!r} before restoring")
        missing = [alias for alias in aliases if not directory.same(alias, directory.node)]
        if missing:
            raise ValueError("Register the backup's missing local aliases before restoring: " +
                             " ".join("--alias " + alias for alias in missing))
        prior = json.loads(journal.read_bytes()) if journal.exists() else None
        if prior is not None and prior.get("sha256") != checksum:
            raise ValueError("This installation already has a different mailbox import")
        if prior and prior["status"] == "completed":
            for name in files:
                database = physical(state / name)
                if not database.is_file():
                    raise ValueError("An imported database is missing")
                check_database(database, name)
            return {**prior, "already_imported": True}
        for name in DATABASES:
            existing = physical(state / name)
            if existing.exists() and (not prior or name not in files or ops.digest(existing) != files[name]["sha256"]):
                raise ValueError(f"Use a fresh target mailbox; existing {name} would conflict")
            if any(physical(state / (name + suffix)).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise ValueError("Close mailbox clients before restoring")
        for name, record in files.items():
            target = scratch / name
            if bundle.getinfo(name).file_size != record["bytes"]:
                raise ValueError("The stored database size differs from the manifest")
            with target.open("xb") as output, bundle.open(name) as data:
                shutil.copyfileobj(data, output)
            if ops.digest(target) != record["sha256"]:
                raise ValueError("The stored database checksum differs from the manifest")
            check_database(target, name)
        receipt = {"sha256": checksum, "device": directory.node, "source_device": source, "identity_key": identity,
                   "source_aliases": aliases, "databases": sorted(files), "status": "importing"}
        journal.parent.mkdir(exist_ok=True)
        ops.write_atomic(journal, ops.canonical_json(receipt))
        for name in files:
            destination = state / name
            if not destination.exists():
                os.rename(scratch / name, destination)
            if ops.digest(destination) != files[name]["sha256"]:
                raise RuntimeError("Destination changed during import; keep the original and the receipt")
        receipt["status"] = "completed"
        ops.write_atomic(journal, ops.canonical_json(receipt))
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    export = sub.add_parser("export")
    export.add_argument("--device", help="Historical device name when exporting an unconfigured legacy installation")
    restore = sub.add_parser("restore")
    restore.add_argument("--sha256", required=True)
    restore.add_argument("--source-device", help="Confirm the previous device name when restoring under a different name")
    for command in (export, restore):
        command.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        raise ValueError("This migration tool is for Windows Pair")
    from verify import verify_directory
    metadata = verify_directory(KIT)
    from install import device_helpers, load_at, physical, read_configuration
    naming = device_helpers(KIT, metadata)
    ops = load_at(KIT / "deploy" / "deploy.py")
    root = physical(args.root or ops.install_root())
    ops.verify_root(root)
    archive = physical(args.archive)
    if archive.is_relative_to(root):
        raise ValueError("Keep the migration archive outside the installation")
    _, config = read_configuration(root / "config.json", naming)
    directory = naming.DeviceDirectory(config)
    if args.action == "export":
        source, aliases, identity = source_identity(config, directory, naming, args.device)
        return export_snapshot(root, archive, source, ops, aliases=aliases, identity_key=identity)
    if not config.get("device") or config["device"] == "local":
        raise ValueError("Install the target product before restoring a mailbox")
    directory.check_machine()
    return restore_snapshot(root, archive, directory, args.sha256, ops, naming, source_device=args.source_device)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(json.dumps({"ok": True, **main()}, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error, zipfile.BadZipFile) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
