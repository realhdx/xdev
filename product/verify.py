"""Verify an xdev product archive or extracted directory using the standard library."""

import argparse
import hashlib
import json
import re
import stat
import sys
import tempfile
import zipfile
from pathlib import Path


MANIFEST = "package-manifest.json"
RUNTIME = "runtime.zip"
DEPENDENCIES = "assets/optional-deps.zip"
ROOT_FILES = {"install.py", "migrate.py", "verify.py", "release.json",
              "README.md", "AI-DEPLOY.md", "CHANGELOG.md"}
DEPLOY_FILES = {"deploy/deploy.py", "deploy/pair.py", "deploy/clients.py",
                "deploy/dsh_roots.py", "deploy/optional.py"}
INTEGRATION_FILES = {"integrations/.codebuddy-plugin/marketplace.json",
                     "integrations/workbuddy/.codebuddy-plugin/plugin.json",
                     "integrations/workbuddy/package.json", "integrations/workbuddy/hooks/hooks.json",
                     "integrations/workbuddy/hooks/receiver.js"}
LEGACY_PACKAGE_FILES = ROOT_FILES | DEPLOY_FILES | INTEGRATION_FILES | {RUNTIME, DEPENDENCIES}
SKILL_FILE = "integrations/skills/xdev-local/SKILL.md"
PROJECT_NOTICES = {"LICENSE", "CITATION.cff"}
ENGLISH_MANUALS = {"README.en.md", "AI-DEPLOY.en.md", "CHANGELOG.en.md"}
RELEASE_NOTES = {"RELEASE-NOTES.md", "RELEASE-NOTES.en.md"}
WORKBUDDY_SKILL = "integrations/workbuddy/skills/xdev-session-mail/SKILL.md"
ISOLATED_BROWSER_EXAMPLE = {"examples/isolated-browser/README.md", "examples/isolated-browser/check.cjs"}
RELAY_GUIDE = "design/session-relay.md"
RELAY_ACL = "deploy/session-relay.conf.example"
MANAGED_QUEUE_GUIDE = "design/managed-queue.md"
GITHUB_MAIL_GUIDE = "design/github-mail.md"
MAIL_LISTENER_SETUP = "deploy/mail-listener.py"
GITHUB_MAIL_SETUP = "deploy/github-mail.py"
HOST_GUIDES = {"design/resident-host.md", "design/host-discovery.md"}
CONTACT_GUIDES = {"design/mail-lifecycle.md", "design/mail-retention.md", "design/paired-folder-mail.md"}
SHA256 = re.compile(r"[0-9a-f]{64}")


def package_filenames(version, documentation_revision=0):
    components = version_components(version)
    if components < (1, 0, 2):
        return LEGACY_PACKAGE_FILES
    files = LEGACY_PACKAGE_FILES | {SKILL_FILE}
    if (*components, documentation_revision) >= (1, 0, 3, 3):
        files |= PROJECT_NOTICES
    if (*components, documentation_revision) >= (1, 0, 3, 5):
        files |= ENGLISH_MANUALS
    if since_network_preview(version, 3):
        files.update({RELAY_GUIDE, RELAY_ACL})
    if since_network_preview(version, 4):
        files.add(MANAGED_QUEUE_GUIDE)
    if since_network_preview(version, 6):
        files.update(HOST_GUIDES)
    if since_network_preview(version, 7):
        files.update({GITHUB_MAIL_GUIDE, GITHUB_MAIL_SETUP})
    if since_network_preview(version, 8):
        files.add(MAIL_LISTENER_SETUP)
    if since_network_preview(version, 12):
        files.update(CONTACT_GUIDES | {"deploy/paired-folder-mail.py", "deploy/paired-folder-worker.py",
                     "deploy/paired-folder-service.ps1", "deploy/github-pr-notice.py", "deploy/mail-retention.py"})
    if since_network_preview(version, 14) or (since_network_preview(version, 13) and documentation_revision >= 1):
        files.update(RELEASE_NOTES | {WORKBUDDY_SKILL} | ISOLATED_BROWSER_EXAMPLE)
    return files


def version_components(version):
    core = re.split(r"[-+]", version, maxsplit=1)[0]
    return tuple(map(int, core.split(".")))


def since_network_preview(version, number):
    """The 1.1.0 preview introduced separately versioned runtime and package files."""
    core = version_components(version)
    if core != (1, 1, 0):
        return core > (1, 1, 0)
    _, _, prerelease = version.split("+", 1)[0].partition("-")
    preview = re.fullmatch(r"preview\.(\d+)", prerelease)
    return not prerelease or bool(preview and int(preview[1]) >= number)


def runtime_filenames(version="1.0.0"):
    """The reviewed runtime tree; adding a deliverable requires an intentional release edit."""
    by_folder = {
        "src": ("product.json",),
        "src/cli": ("main.py", "letters.py"),
        "src/runtime": ("cli_result.py", "commands.py", "correspondence.py", "device_names.py", "jobs.py", "letter_attachment.py",
                        "local_ipc.py", "messages.py", "subjects.py", "transport.py"),
        "src/file_layer": ("__init__.py", "common.py", "fs_ops.py", "remote_fs_helper.py", "transfer.py", "xdev_file.py"),
        "src/adapters": ("antigravity.py", "caller_context.py", "claude.py", "claude_channel.py", "codebuddy_channel.py",
                         "codex.py", "codex_native.py", "codex_receiver.py", "delivery_contract.py", "dsh.py",
                         "dsh_host_tool.mjs", "dsh_host_tool.py", "inprocess_cli.py", "mcp_stdio.py", "notices.py",
                         "official_channel.py", "opencode.py", "sessions.py", "workbuddy.py"),
        "src/adapters/workbuddy_plugin": ("hooks.json", "package.json", "plugin.json", "receiver.js"),
    }
    files = {f"{directory}/{filename}" for directory, names in by_folder.items() for filename in names}
    if version_components(version) >= (1, 1, 0):
        files.update({"src/runtime/surfaces.py", "src/adapters/surface_browser.py",
                      "src/adapters/surface_windows.py", "src/adapters/surface_windows.ps1"})
    if since_network_preview(version, 3):
        files.update({"src/runtime/session_relay.py", "src/runtime/outbox.py", "src/adapters/portable.py"})
    if since_network_preview(version, 4):
        files.update({"src/runtime/ssh_mail.py", "src/runtime/managed_queue.py"})
    if since_network_preview(version, 6):
        files.update({"src/adapters/resident.py", "src/adapters/host_catalog.py", "src/adapters/mcp_catalog.py",
                      "src/runtime/mail_routes.py"})
    if since_network_preview(version, 7):
        files.add("src/runtime/github_mail.py")
    if since_network_preview(version, 10):
        files.add("src/runtime/github_demand.py")
    if since_network_preview(version, 11):
        files.update({"src/adapters/mail_arrival.py", "src/adapters/mail_arrival_stdio.py"})
    if since_network_preview(version, 12):
        files.update({"src/cli/contact.py", "src/runtime/mail_control.py", "src/runtime/mail_control_ssh.py",
                      "src/runtime/mail_lifecycle.py", "src/runtime/mail_retention.py", "src/runtime/mail_contact.py",
                      "src/runtime/github_capacity.py"})
    return files


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def strict_json(contents):
    def object_pairs(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate JSON key")
        return result
    return json.loads(contents, object_pairs_hook=object_pairs)


def metadata(opener, name):
    with opener(name) as stream:
        data = stream.read(1_048_577)
    if len(data) > 1_048_576:
        raise ValueError(f"Oversized metadata: {name}")
    return strict_json(data)


def validate_release(release):
    if (not isinstance(release, dict) or type(release.get("schema")) is not int or release["schema"] != 1
            or release.get("product") != "xdev"):
        raise ValueError("Invalid xdev product release metadata")
    for field, pattern in (("series", r"[a-z0-9]+(?:-[a-z0-9]+)*"),
                           ("version", r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?")):
        if not isinstance(release.get(field), str) or not re.fullmatch(pattern, release[field]):
            raise ValueError(f"Invalid release {field}")
    for field in ("runtime_sha256", "optional_deps_sha256"):
        if not isinstance(release.get(field), str) or not SHA256.fullmatch(release[field]):
            raise ValueError(f"Invalid release {field}")
    if "documentation_revision" in release:
        revision = release["documentation_revision"]
        if type(revision) is not int or revision < 1:
            raise ValueError("documentation_revision must be a positive integer")
    return release


def check_files(opener, manifest, expected, sizes):
    if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest["schema"] != 1
            or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != expected):
        raise ValueError("Manifest file list differs from the reviewed product layout")
    for name, record in manifest["files"].items():
        if (not isinstance(record, dict) or set(record) != {"sha256", "size"} or type(record["size"]) is not int
                or record["size"] != sizes[name] or not isinstance(record["sha256"], str)
                or not SHA256.fullmatch(record["sha256"])):
            raise ValueError(f"Invalid manifest record or length: {name}")
        with opener(name) as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != record["sha256"]:
            raise ValueError(f"SHA-256 mismatch: {name}")


def zip_sizes(archive, expected):
    entries = archive.infolist()
    if len(entries) != len(expected) or {i.filename for i in entries} != expected:
        raise ValueError("Archive contains missing, additional, or duplicate entries")
    for entry in entries:
        kind = stat.S_IFMT(entry.external_attr >> 16)
        if entry.is_dir() or kind not in (0, stat.S_IFREG) or entry.flag_bits & 1:
            raise ValueError(f"Invalid archive file type: {entry.filename}")
    return {entry.filename: entry.file_size for entry in entries}


def verify_runtime(handle, release):
    with zipfile.ZipFile(handle) as archive:
        files = runtime_filenames(release["version"])
        sizes = zip_sizes(archive, files | {"release-manifest.json"})
        check_files(archive.open, metadata(archive.open, "release-manifest.json"), files, sizes)
        return metadata(archive.open, "src/product.json")


def verify_payload(opener, sizes, release):
    manifest = metadata(opener, MANIFEST)
    check_files(opener, manifest, package_filenames(release["version"], release.get("documentation_revision", 0)), sizes)
    if release["runtime_sha256"] != manifest["files"][RUNTIME]["sha256"]:
        raise ValueError("The runtime digest differs from release.json")
    if release["optional_deps_sha256"] != manifest["files"][DEPENDENCIES]["sha256"]:
        raise ValueError("The dependency digest differs from release.json")
    with opener(RUNTIME) as source, tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024) as buffer:
        while block := source.read(262144):
            buffer.write(block)
        buffer.seek(0)
        identity = verify_runtime(buffer, release)
    if identity != {k: release[k] for k in ("product", "series", "version")}:
        raise ValueError("The runtime product identity differs from release.json")
    return release


def verify_archive(path, expected_sha256=None):
    path = Path(path)
    if expected_sha256 is not None and file_digest(path) != expected_sha256.lower():
        raise ValueError("Product archive differs from the supplied SHA-256")
    with zipfile.ZipFile(path) as archive:
        if "release.json" not in archive.namelist():
            raise ValueError("Archive is missing release.json")
        release = validate_release(metadata(archive.open, "release.json"))
        expected = package_filenames(release["version"], release.get("documentation_revision", 0))
        sizes = zip_sizes(archive, expected | {MANIFEST})
        return verify_payload(archive.open, sizes, release)


def physical(path):
    if path.is_symlink() or path.is_junction():
        raise ValueError(f"Linked package path: {path}")


def verify_directory(directory):
    directory = Path(directory)
    physical(directory)
    if not directory.is_dir():
        raise ValueError("Expected an extracted product directory")
    sizes = {}
    release_path = directory / "release.json"
    physical(release_path)
    if not release_path.is_file():
        raise ValueError("Extracted product is missing release.json")
    def opener(name):
        return (directory / name).open("rb")

    release = validate_release(metadata(opener, "release.json"))
    allowed = package_filenames(release["version"], release.get("documentation_revision", 0)) | {MANIFEST}
    allowed_dirs = {parent.as_posix() for name in allowed for parent in Path(name).parents if parent != Path(".")}

    def raise_walk_error(error):
        raise error

    for folder, directories, filenames in directory.walk(on_error=raise_walk_error):
        for child in directories:
            path = folder / child
            physical(path)
            if path.relative_to(directory).as_posix() not in allowed_dirs:
                raise ValueError(f"Unexpected package directory: {path.name}")
        for child in filenames:
            path = folder / child
            physical(path)
            if not path.is_file():
                raise ValueError(f"Invalid package file: {path.name}")
            sizes[path.relative_to(directory).as_posix()] = path.stat().st_size
    if set(sizes) != allowed:
        raise ValueError("Extracted product contains missing or additional files")
    return verify_payload(opener, sizes, release)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--sha256", help="Expected SHA-256 of a product ZIP")
    args = parser.parse_args()
    if args.path.is_dir():
        if args.sha256:
            parser.error("--sha256 applies to a ZIP, not an extracted directory")
        release = verify_directory(args.path)
    else:
        release = verify_archive(args.path, args.sha256)
    result = {"ok": True, "product": release["product"], "series": release["series"],
              "version": release["version"], "runtime_sha256": release["runtime_sha256"]}
    if "documentation_revision" in release:
        result["documentation_revision"] = release["documentation_revision"]
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
