"""Package and install the pinned Windows MCP and NATS client dependencies."""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from uuid import uuid4
import zipfile


APP = "CrossDeviceCollab"
MARKER = ".xdev-install.json"
RECEIPT = ".xdev-deps.json"
LOCK = "requirements.lock.txt"
MANIFEST = "bundle-manifest.json"
SUMS = "SHA256SUMS.txt"
PINNED = {
    LOCK: "9809570204f1d0f699dbddfdca47655a3e323a766c2edbc43bef656f709878c6",
    MANIFEST: "b931717a539e3f7c748d81a9b4a6556e9126befde79fdc89eb56387c2e178209",
    SUMS: "0677b7ad835d299baa9d7148d76a4da6fc4d904ecb71f97a02ad5a6adfed6e7e",
}
PACKAGES = {"mcp": "1.27.1", "nats-py": "2.14.0"}
WHEEL_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.+-]*\.whl")
SHA = re.compile(r"[a-f0-9]{64}")
SSH_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def sha256(value):
    if not SHA.fullmatch(value):
        raise argparse.ArgumentTypeError("Expected a lower-case SHA-256 checksum")
    return value


def check_physical(path):
    absolute = Path(os.path.abspath(path))
    for entry in (absolute, *absolute.parents):
        if entry.is_symlink() or entry.is_junction():
            raise ValueError(f"Linked optional dependency path: {entry}")
    return absolute


def read_small(path, limit):
    path = check_physical(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"Missing or oversized optional dependency metadata: {path.name}")
    return path.read_bytes()


def verify_root(path):
    root = check_physical(path)
    if not root.is_dir() or str(root).startswith("\\\\"):
        raise ValueError("Expected a machine-local xdev installation")
    if json.loads(read_small(root / MARKER, 1024)) != {"app": APP, "schema": 1}:
        raise ValueError("Invalid xdev installation marker")
    for name in ("releases", "state"):
        if not check_physical(root / name).is_dir():
            raise ValueError(f"The xdev installation has no {name} directory")
    return root


def verify_metadata(metadata):
    for name, expected in PINNED.items():
        if hashlib.sha256(metadata[name]).hexdigest() != expected:
            raise ValueError(f"Optional dependency {name} differs from the pinned release")
    manifest = json.loads(metadata[MANIFEST])
    if manifest.get("platform") != "win_amd64" or manifest.get("python") != "3.14":
        raise ValueError("Expected a Windows x64 Python 3.14 bundle")
    records = {}
    for item in manifest["wheels"]:
        name = item["file"]
        if not WHEEL_NAME.fullmatch(name) or name.casefold() in records:
            raise ValueError("Unsafe or duplicate dependency wheel name")
        records[name.casefold()] = item
    if len(records) != 31:
        raise ValueError("Unexpected optional dependency wheel set")
    versions = {item["project"]: item["version"] for item in records.values()}
    if any(versions.get(name) != version for name, version in PACKAGES.items()):
        raise ValueError("Unexpected optional dependency versions")
    return {f"wheelhouse/{item['file']}": item for item in records.values()}


def package(bundle, archive):
    bundle = check_physical(bundle)
    archive = check_physical(archive)
    if archive.exists() or archive.is_relative_to(bundle):
        raise ValueError("Write the archive to a new path outside the wheel bundle")
    metadata = {name: read_small(bundle / name, 128 * 1024) for name in PINNED}
    wheels = verify_metadata(metadata)
    source_dir = check_physical(bundle / "wheelhouse")
    if {path.name for path in source_dir.iterdir()} != {item["file"] for item in wheels.values()}:
        raise ValueError("The wheel directory contains missing or additional files")
    with tempfile.NamedTemporaryFile(prefix=".xdev-wheels-", dir=archive.parent, delete=False) as file:
        temporary = Path(file.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as target:
            for name, content in sorted(metadata.items()):
                target.writestr(zipfile.ZipInfo(name), content)
            for name, record in sorted(wheels.items()):
                wheel = check_physical(bundle / name)
                if wheel.stat().st_size != record["size"]:
                    raise ValueError(f"Wheel size mismatch: {wheel.name}")
                body = wheel.read_bytes()
                if hashlib.sha256(body).hexdigest() != record["sha256"]:
                    raise ValueError(f"Wheel checksum mismatch: {wheel.name}")
                target.writestr(zipfile.ZipInfo(name), body)
        # Windows rename does not replace an existing file.
        os.rename(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    return {"archive": str(archive), "sha256": digest(archive), "wheels": len(wheels)}


def extract(archive, scratch):
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        entries = {entry.filename: entry for entry in members}
        if len(entries) != len(members) or not PINNED.keys() <= entries.keys():
            raise ValueError("Missing or duplicate dependency archive entries")
        if any(entries[name].file_size > 128 * 1024 for name in PINNED):
            raise ValueError("Oversized dependency metadata")
        metadata = {name: source.read(entries[name]) for name in PINNED}
        wheels = verify_metadata(metadata)
        if set(entries) != PINNED.keys() | wheels.keys():
            raise ValueError("Unexpected dependency archive entries")
        (scratch / "wheelhouse").mkdir()
        for name, item in wheels.items():
            entry = entries[name]
            if entry.file_size != item["size"]:
                raise ValueError(f"Incorrect wheel length: {name}")
            content = source.read(entry)
            if hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise ValueError(f"Wheel checksum mismatch: {name}")
            (scratch / name).write_bytes(content)
    # The installer validates this exact lock digest before executing pip.
    (scratch / LOCK).write_bytes(metadata[LOCK])


@contextlib.contextmanager
def deployment_lock(root):
    import msvcrt

    path = check_physical(root / "deploy.lock")
    with path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        except OSError:
            raise RuntimeError("Another xdev process owns the deployment lock") from None
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def run(arguments, timeout, env=None):
    try:
        process = subprocess.run(
            arguments, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=timeout, env=env, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Optional dependency installation or validation timed out") from None
    if process.returncode:
        output = (process.stderr or process.stdout).decode("utf-8", "replace")
        raise RuntimeError(f"Optional dependency check failed: {output.strip()[-1800:]}")


def verify_imports(target):
    source = """
import importlib.metadata as metadata
from pathlib import Path
import site
import sys
root = Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0, str(root))
site.addsitedir(str(root))
import mcp, nats, pywintypes
from mcp.server.fastmcp import FastMCP
from nats.js import JetStreamContext
for module in (mcp, nats, pywintypes):
    if not Path(module.__file__).resolve().is_relative_to(root):
        raise SystemExit("Optional package came from outside the xdev site")
if metadata.version("mcp") != "1.27.1" or metadata.version("nats-py") != "2.14.0":
    raise SystemExit("Optional package version mismatch")
FastMCP("xdev-dependency-verification")
"""
    run([sys.executable, "-I", "-B", "-X", "utf8", "-c", source, str(target)], timeout=25)


def inventory(target):
    files = {}
    for directory, children, names in target.walk():
        for name in [*children, *names]:
            check_physical(directory / name)
        for name in names:
            file = directory / name
            relative = file.relative_to(target).as_posix()
            if relative != RECEIPT:
                files[relative] = digest(file)
    return files


def verify_existing(destination, checksum):
    if not destination.is_dir() or not (destination / RECEIPT).is_file():
        raise ValueError("The existing deps path is not managed by this installer")
    record = json.loads(read_small(destination / RECEIPT, 2 * 1024 * 1024))
    expected = {"schema": 1, "archive_sha256": checksum, "lock_sha256": PINNED[LOCK],
                "packages": PACKAGES}
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("The existing deps directory belongs to a different dependency release")
    if not isinstance(record.get("files"), dict) or inventory(destination) != record["files"]:
        raise ValueError("The existing deps directory has been changed")
    verify_imports(destination)


def check_archive(path, checksum):
    archive = check_physical(path)
    if not archive.is_file() or archive.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Missing or oversized dependency archive")
    if digest(archive) != checksum:
        raise ValueError("Optional archive checksum mismatch")
    return archive


def install(root, path, checksum):
    if sys.platform != "win32" or sys.version_info[:2] != (3, 14):
        raise RuntimeError("The xdev dependency bundle requires Windows Python 3.14")
    if sysconfig.get_platform().lower() != "win-amd64":
        raise RuntimeError("The xdev optional bundle targets Windows x64")
    root = verify_root(root)
    archive = check_archive(path, checksum)
    destination = check_physical(root / "deps")
    with deployment_lock(root):
        if destination.exists():
            verify_existing(destination, checksum)
            reused = True
        else:
            scratch = root / f".xdev-optional-{uuid4().hex}"
            scratch.mkdir()
            try:
                if check_physical(scratch).resolve(strict=True) != scratch or scratch.parent != root:
                    raise ValueError("Dependency staging path is outside the managed root")
                extract(archive, scratch)
                pip_target = scratch / "pip-site"
                run(
                    [sys.executable, "-X", "utf8", "-m", "pip", "--isolated",
                     "--disable-pip-version-check", "--no-input", "install", "--no-index",
                     "--find-links", str(scratch / "wheelhouse"), "--only-binary=:all:",
                     "--require-hashes", "--ignore-installed", "--no-compile", "--target",
                     str(pip_target), "-r", str(scratch / LOCK)],
                    timeout=55,
                    env={**os.environ, "PIP_CONFIG_FILE": os.devnull},
                )
                # pip can move files and directories out of its own protected
                # temporary tree. Copy bytes into a fresh inheriting tree before
                # publishing for the signed-in user's limited token.
                target = scratch / "site"
                installed_files = inventory(pip_target)
                shutil.copytree(pip_target, target, copy_function=shutil.copyfile)
                if inventory(target) != installed_files:
                    raise ValueError("Optional dependency bytes changed during staging")
                verify_imports(target)
                receipt = {"schema": 1, "archive_sha256": checksum, "lock_sha256": PINNED[LOCK],
                           "packages": PACKAGES, "files": installed_files}
                (target / RECEIPT).write_text(
                    json.dumps(receipt, sort_keys=True), encoding="utf-8", newline="\n",
                )
                os.rename(target, destination)
                reused = False
            finally:
                if check_physical(scratch).resolve(strict=True) == scratch and scratch.parent == root:
                    shutil.rmtree(scratch)
    return {"archive_sha256": checksum, "lock_sha256": PINNED[LOCK], "packages": PACKAGES,
            "dependencies": str(destination), "reused": reused}


def install_pair(args, root):
    # This local-only import is intentionally absent when the script runs on
    # the peer as its one-file deploy.py.
    from pair import Peer

    if not SSH_HOST.fullmatch(args.host):
        raise ValueError("Expected an existing SSH host alias")
    root = verify_root(root)
    archive = check_archive(args.archive, args.sha256)
    if (root / "deps").exists():
        verify_existing(check_physical(root / "deps"), args.sha256)
    peer = Peer(args.host, args.remote_root)
    script = Path(__file__).resolve(strict=True)
    with peer.staging():
        peer.copy(script, archive)
        remote = peer.command(
            digest(script), "install", "--archive", peer.directory / "release.zip", "--sha256", args.sha256,
        )
    if remote.get("archive_sha256") != args.sha256 or remote.get("lock_sha256") != PINNED[LOCK]:
        raise RuntimeError("Peer did not return the expected optional dependency receipt")
    try:
        local = install(root, archive, args.sha256)
    except (ValueError, OSError, RuntimeError) as exc:
        raise RuntimeError(f"Peer installed the bundle; local installation incomplete: {exc}") from None
    return {"local": local, "peer": remote}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Managed local installation root")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("pack")
    build.add_argument("--bundle", type=Path, required=True)
    build.add_argument("--archive", type=Path, required=True)
    for name in ("install", "pair"):
        command = commands.add_parser(name)
        command.add_argument("--archive", type=Path, required=True)
        command.add_argument("--sha256", type=sha256, required=True)
    commands.choices["pair"].add_argument("--host", required=True)
    commands.choices["pair"].add_argument("--remote-root")
    args = parser.parse_args()
    if args.command == "pack":
        return package(args.bundle, args.archive)
    root = args.root or Path(os.environ["LOCALAPPDATA"]) / APP
    return install_pair(args, root) if args.command == "pair" else install(root, args.archive, args.sha256)


if __name__ == "__main__":
    try:
        print(json.dumps({"ok": True, **main()}, ensure_ascii=False))
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
