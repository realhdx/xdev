"""Build, verify, and activate an immutable xdev release for one Windows user."""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import time
from uuid import uuid4
import zipfile


APP = "CrossDeviceCollab"
MARKER = ".xdev-install.json"
MANIFEST = "release-manifest.json"
RELEASE_ID = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_TREES = {"src", "bin"}
SKIP = {"__pycache__", ".git", ".venv", "node_modules", "tmp", "temp"}
REQUIRED = {"src/cli/main.py"}

LAUNCHER = '''"""Locate the active xdev release without changing the caller's directory."""
import os
from pathlib import Path
import re
import runpy
import site
import sys

root = Path(__file__).resolve().parent
try:
    version = (root / "current.txt").read_text(encoding="ascii").splitlines()[0]
except (OSError, UnicodeError, IndexError):
    raise SystemExit("xdev: no active release")
if not re.fullmatch("[0-9a-f]{64}", version):
    raise SystemExit("xdev: invalid release pointer")
release = root / "releases" / version
if release.resolve() != release or not (release / "src" / "cli" / "main.py").is_file():
    raise SystemExit("xdev: active release is unavailable")
os.environ["XDEV_HOME"] = str(root)
dependencies = root / "deps"
if dependencies.is_dir():
    sys.path.insert(0, str(dependencies))
    site.addsitedir(str(dependencies))
sys.path.insert(0, str(release))
runpy.run_module("src.cli.main", run_name="__main__", alter_sys=True)
'''


def install_root():
    return Path(os.environ["LOCALAPPDATA"]) / APP


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def release_id(value):
    if not isinstance(value, str) or not RELEASE_ID.fullmatch(value):
        raise ValueError("Expected a SHA-256 release ID")
    return value


def real_entry(path):
    if path.is_symlink() or path.is_junction():
        raise ValueError(f"Linked paths are not packaged or installed: {path}")


def source_files(root):
    paths = []
    for tree in sorted(SOURCE_TREES):
        source = root / tree
        if not source.is_dir():
            continue
        real_entry(source)
        for directory, children, files in source.walk():
            children[:] = sorted(x for x in children if x not in SKIP)
            for name in children + files:
                real_entry(directory / name)
            paths.extend(
                directory / name for name in sorted(files)
                if name not in SKIP and not name.endswith(".pyc")
            )
    return sorted(paths)


def package_path(name):
    parts = name.split("/")
    if len(parts) < 2 or parts[0] not in SOURCE_TREES:
        raise ValueError(f"Unexpected release path: {name!r}")
    for part in parts:
        basename = part.split(".")[0].upper()
        reserved = basename in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch("(?:COM|LPT)[1-9¹²³]", basename)
        if (
            not part or part in SKIP or part.endswith((" ", ".")) or reserved
            or any(ord(char) < 32 or char in '\\:<>|"?*' for char in part)
        ):
            raise ValueError(f"Unsafe Windows release path: {name!r}")
    return Path(*PurePosixPath(name).parts)


def write_atomic(path, data):
    handle, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build(source, output):
    source = source.resolve(strict=True)
    output = output.resolve()
    files = source_files(source)
    names = {path.relative_to(source).as_posix() for path in files}
    if not REQUIRED <= names:
        raise ValueError("The source tree has no src/cli/main.py")
    if output.is_relative_to(source / "src") or output.is_relative_to(source / "bin"):
        raise ValueError("Store the bundle outside src and bin")
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".xdev-bundle-", dir=output.parent)
    temporary = Path(name)
    os.close(handle)
    manifest = {"schema": 1, "files": {}}
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for path in files:
                name = path.relative_to(source).as_posix()
                package_path(name)
                content = path.read_bytes()
                manifest["files"][name] = {
                    "size": len(content), "sha256": hashlib.sha256(content).hexdigest()
                }
                archive.writestr(name_record(name), content)
            archive.writestr(name_record(MANIFEST), canonical_json(manifest))
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"archive": str(output), "release": digest(output), "files": len(files)}


def name_record(name):
    record = zipfile.ZipInfo(name)
    record.create_system = 0
    record.compress_type = zipfile.ZIP_DEFLATED
    return record


def load_manifest(raw):
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise ValueError("Invalid release manifest version")
    files = manifest.get("files")
    if not isinstance(files, dict) or not REQUIRED <= files.keys():
        raise ValueError("The manifest has no xdev entry point")
    folded = set()
    for name, info in files.items():
        package_path(name)
        if name.casefold() in folded:
            raise ValueError(f"Case-insensitive path collision: {name}")
        folded.add(name.casefold())
        if not isinstance(info, dict) or type(info.get("size")) is not int or info["size"] < 0:
            raise ValueError(f"Invalid file length for {name}")
        release_id(info.get("sha256", ""))
    return files


def initialize(root):
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    if not marker.is_file():
        if any(root.iterdir()):
            raise ValueError(f"Refusing to adopt an existing non-xdev directory: {root}")
        write_atomic(marker, canonical_json({"app": APP, "schema": 1}))
    verify_root(root)
    for name in ("releases", "state"):
        directory = root / name
        real_entry(directory)
        directory.mkdir(exist_ok=True)


def verify_root(root):
    marker = root / MARKER
    real_entry(marker)
    if not marker.is_file() or json.loads(marker.read_bytes()) != {"app": APP, "schema": 1}:
        raise ValueError(f"Not an xdev installation: {root}")
    for name in ("releases", "state", "current.txt", "launch.py", "xdev.cmd", "deps"):
        real_entry(root / name)


@contextlib.contextmanager
def deployment_lock(root):
    import msvcrt

    lock = root / "deploy.lock"
    real_entry(lock)
    with lock.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\x00")
            stream.flush()
        deadline = time.monotonic() + 30
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another deployment still owns the lock") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def read_pointer(root):
    path = root / "current.txt"
    if not path.exists():
        return None, None
    real_entry(path)
    lines = path.read_text(encoding="ascii").splitlines()
    if len(lines) != 2:
        raise ValueError("Invalid xdev release pointer")
    return tuple(release_id(line) if line else None for line in lines)


def check_release(directory, version):
    real_entry(directory)
    manifest = directory / MANIFEST
    source_id = directory / ".archive-sha256"
    real_entry(manifest)
    real_entry(source_id)
    if source_id.read_text(encoding="ascii") != version:
        raise ValueError(f"Release receipt does not match {version}")
    expected = load_manifest(manifest.read_bytes())
    paths = {p.relative_to(directory).as_posix(): p for p in source_files(directory)}
    if set(paths) != set(expected):
        raise ValueError(f"Installed release has missing or additional source files: {version}")
    for name, path in paths.items():
        if path.stat().st_size != expected[name]["size"] or digest(path) != expected[name]["sha256"]:
            raise ValueError(f"Installed file checksum failed: {name}")


def extract(archive_path, staging, version):
    manifest_path = staging / MANIFEST
    with zipfile.ZipFile(archive_path, "r") as archive:
        items = {entry.filename: entry for entry in archive.infolist()}
        if len(items) != len(archive.infolist()) or MANIFEST not in items:
            raise ValueError("Archive manifest is missing or includes duplicate names")
        raw = archive.read(MANIFEST)
        files = load_manifest(raw)
        if set(items) - {MANIFEST} != set(files):
            raise ValueError("Archive entries differ from the manifest")
        for name, info in files.items():
            entry = items[name]
            if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16) or entry.file_size != info["size"]:
                raise ValueError(f"Unexpected archive file type or size: {name}")
            path = staging / package_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, path.open("xb") as target:
                shutil.copyfileobj(source, target)
            if digest(path) != info["sha256"]:
                raise ValueError(f"Archive checksum failed: {name}")
        manifest_path.write_bytes(raw)
        (staging / ".archive-sha256").write_text(version, encoding="ascii")


def ensure_launcher(root):
    python = sys.executable.replace("%", "%%")
    command = (
        "@echo off\r\n"
        "setlocal DisableDelayedExpansion\r\n"
        f'"{python}" -I -B -X utf8 "%~dp0launch.py" %*\r\n'
        "exit /b %errorlevel%\r\n"
    ).encode("utf-8")
    if not python.isascii():
        raise ValueError("Use a Python interpreter installed at an ASCII path for the Windows launcher")
    for path, data in ((root / "launch.py", LAUNCHER.encode("utf-8")), (root / "xdev.cmd", command)):
        real_entry(path)
        if not path.exists() or path.read_bytes() != data:
            write_atomic(path, data)


def publish_release(staging, target):
    # A Windows indexer or scanner can briefly hold the just-written directory.
    for delay in (0.05, 0.1, 0.2, 0.4, 0.8, None):
        if os.path.lexists(target):
            raise FileExistsError(f"Release destination already exists: {target}")
        try:
            os.rename(staging, target)
            return
        except OSError as error:
            if delay is None or getattr(error, "winerror", None) not in {5, 32, 33}:
                raise
            time.sleep(delay)


def stage(root, archive, version):
    if digest(archive) != version:
        raise ValueError("Release archive SHA-256 does not match")
    initialize(root)
    with deployment_lock(root):
        releases = root / "releases"
        target = releases / version
        real_entry(target)
        if target.exists():
            check_release(target, version)
        else:
            # Python 3.13+ Windows TemporaryDirectory creates a protected
            # owner/administrators-only DACL. An elevated SSH token can make
            # Administrators the owner, excluding the signed-in user's
            # limited token after the directory is renamed into production.
            staging = releases / f".staging-{uuid4().hex}"
            staging.mkdir()
            try:
                if staging.resolve(strict=True).parent != releases or target.parent != releases:
                    raise ValueError("Unexpected release staging path")
                extract(archive, staging, version)
                check_release(staging, version)
                publish_release(staging, target)
            finally:
                if staging.exists():
                    real_entry(staging)
                    if staging.resolve(strict=True).parent != releases:
                        raise ValueError("Unexpected release cleanup path")
                    shutil.rmtree(staging)
        ensure_launcher(root)
        current, previous = read_pointer(root)
    return {"root": str(root), "release": version, "active": current, "previous": previous}


def change_pointer(root, selected, expect=None, rollback=False):
    verify_root(root)
    with deployment_lock(root):
        current, previous = read_pointer(root)
        if expect is not None and current != (None if expect == "none" else release_id(expect)):
            raise RuntimeError(f"Active release changed during the deployment: {current}")
        if rollback and selected is None:
            selected = previous
            if selected is None:
                raise ValueError("No previous release recorded")
        if selected is not None:
            selected = release_id(selected)
            check_release(root / "releases" / selected, selected)
        if selected != current:
            previous = current
            write_atomic(root / "current.txt", f"{selected or ''}\n{previous or ''}\n".encode("ascii"))
    return {"root": str(root), "active": selected, "previous": previous}


def status(root, verify):
    if not root.exists():
        return {"root": str(root), "active": None, "previous": None, "releases": []}
    verify_root(root)
    current, previous = read_pointer(root)
    releases = sorted(
        entry.name for entry in (root / "releases").iterdir()
        if RELEASE_ID.fullmatch(entry.name) and entry.is_dir()
    )
    if verify and current:
        check_release(root / "releases" / current, current)
    return {
        "root": str(root), "active": current, "previous": previous,
        "releases": releases, "active_verified": bool(verify and current)
    }


def parser():
    interface = argparse.ArgumentParser(description=__doc__)
    interface.add_argument("--root", type=Path, help="Installation root, default LOCALAPPDATA/CrossDeviceCollab")
    commands = interface.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser("bundle", help="Build deterministic src/bin archive and return SHA-256")
    bundle.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    bundle.add_argument("--out", type=Path, required=True)
    install = commands.add_parser("stage", help="Verify and store an immutable candidate release")
    install.add_argument("--archive", type=Path, required=True)
    install.add_argument("--sha256", required=True, type=release_id)
    activate = commands.add_parser("activate", help="Atomically select a verified staged release")
    activate.add_argument("release", type=release_id)
    activate.add_argument("--expect-current", metavar="SHA256|none")
    rollback = commands.add_parser("rollback", help="Activate a verified previous release")
    rollback.add_argument("--release", type=release_id)
    rollback.add_argument("--expect-current", metavar="SHA256|none")
    deactivate = commands.add_parser("deactivate", help="Disable entry point and retain code, configuration, and state")
    deactivate.add_argument("--expect-current", metavar="SHA256|none")
    check = commands.add_parser("status", help="Show the active and staged releases")
    check.add_argument("--verify", action="store_true")
    return interface


def main():
    options = parser().parse_args()
    if options.command == "bundle":
        return build(options.source, options.out)
    root = (options.root or install_root()).resolve()
    if options.command == "stage":
        return stage(root, options.archive, options.sha256)
    if options.command == "status":
        return status(root, options.verify)
    return change_pointer(root, getattr(options, "release", None), options.expect_current, options.command == "rollback")


if __name__ == "__main__":
    try:
        result = main()
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
