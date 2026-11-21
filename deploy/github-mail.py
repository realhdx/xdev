"""Initialize or inspect a dedicated private GitHub release used by xdev mail."""

import argparse
import getpass
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from urllib.parse import quote

DESCRIPTION = "Dedicated xdev private mailbox (xdev.github-mail.v1)."
TAG = "xdev-mailbox-v1"
REPO = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}")
API_VERSION = "2022-11-28"


class SetupError(Exception):
    """An operator-facing message. Never include raw bodies, headers or secrets."""


# Windows grants the owner and SYSTEM access to the staged file. Parent checks
# reject directory writers that could substitute another file after publication.
_WINDOWS_ACL = """
$ErrorActionPreference='Stop'
$path=$env:XDEV_MAIL_SETUP_PATH
$kind=$env:XDEV_MAIL_SETUP_ACCESS
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($kind -eq 'protect') {
  $a=[IO.File]::GetAccessControl($path)
  $a.SetSecurityDescriptorSddlForm("D:P(A;;FA;;;$sid)(A;;FA;;;SY)",[Security.AccessControl.AccessControlSections]::Access)
  [IO.File]::SetAccessControl($path,$a)
}
$a=if ($kind -eq 'directory') { [IO.Directory]::GetAccessControl($path) } else { [IO.File]::GetAccessControl($path) }
$trusted=@($sid,'S-1-5-18','S-1-5-32-544')
if ($trusted -notcontains $a.GetOwner([Security.Principal.SecurityIdentifier]).Value) { exit 3 }
$trusted+='S-1-3-4' # OWNER RIGHTS resolves to the owner already verified above.
$unsafe=[Security.AccessControl.FileSystemRights]'Write,DeleteSubdirectoriesAndFiles,Delete,ChangePermissions,TakeOwnership'
foreach ($e in $a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
  if ($e.AccessControlType -ne 'Allow' -or $trusted -contains $e.IdentityReference.Value) { continue }
  if (($e.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0) { continue }
  if ($kind -ne 'directory' -or ($e.FileSystemRights -band $unsafe) -ne 0) { exit 4 }
}
"""


def windows_access(path, kind):
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = {**os.environ, "XDEV_MAIL_SETUP_PATH": str(path), "XDEV_MAIL_SETUP_ACCESS": kind}
    result = subprocess.run(
        [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_ACL],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise SetupError("Cannot establish private access. Choose a separate folder under your user profile.")


def hidden_token():
    if not sys.stdin.isatty():
        raise SetupError("Run save-token in your own interactive terminal for hidden input.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("GitHub repository-scoped token (hidden): ")
        except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
            raise SetupError("Hidden input was unavailable or cancelled.") from None


def save_token(raw_path):
    if not raw_path:
        raise SetupError("save-token requires --token-file and a new absolute filename.")
    path = Path(raw_path)
    profile = Path.home().resolve()
    if (
        not path.is_absolute()
        or not path.is_relative_to(profile)
        or path == profile
        or ".." in path.parts
        or (os.name == "nt" and (path.drive.startswith("\\\\") or any(":" in s for s in path.parts[1:])))
    ):
        raise SetupError("Choose a new local credential file under your user profile.")
    try:
        for folder in (path.parent, *path.parent.parents):
            if (folder / ".git").exists():
                raise SetupError("Store the credential outside any Git repository.")
            if folder.is_symlink() or (folder.exists() and getattr(folder.lstat(), "st_file_attributes", 0) & 0x400):
                raise SetupError("Choose an ordinary directory without links or junctions.")
            if folder == profile:
                break
        if os.path.lexists(path):
            raise SetupError("The credential file already exists. Choose a new filename.")
        if os.name == "nt" and sys.version_info < (3, 13):
            raise SetupError("save-token on Windows needs Python 3.13+ for private directory creation.")
        for folder in reversed([p for p in path.parents if p != profile and p.is_relative_to(profile)]):
            folder.mkdir(mode=0o700, exist_ok=True)
        if os.name == "nt":
            windows_access(path.parent, "directory")
        elif path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o022:
            raise SetupError("Choose a directory writable only by its owner.")
        with tempfile.TemporaryDirectory(prefix=".xdev-token-", dir=path.parent) as staging:
            staged = Path(staging) / "token"
            fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                if os.name == "nt":
                    windows_access(staged, "protect")
                value = hidden_token()
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,4096}", value):
                    raise SetupError("The credential should be a single GitHub access token.")
                stream.write(value.encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            # A hard link publishes the complete, protected inode without ever
            # replacing a path that appeared during the interactive prompt.
            os.link(staged, path)
    except FileExistsError:
        raise SetupError("The credential file already exists. Choose a new filename.") from None
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError(
            "Could not create the private credential file. Check directory access and filesystem hard-link support."
        ) from None
    return {"ok": True, "credential_saved": True}


def credential(path):
    if path is None:
        gh = shutil.which("gh")
        if not gh:
            raise SetupError("Install GitHub CLI and run gh auth login, or supply --token-file.")
        # gh sometimes emits diagnostics containing authentication details. Only the
        # successful token is used, in memory, and neither output stream is forwarded.
        try:
            result = subprocess.run(
                [gh, "auth", "token", "--hostname", "github.com"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SetupError("Could not access the GitHub CLI login. Use gh auth login or --token-file.") from None
        if result.returncode:
            raise SetupError("No usable GitHub CLI login. Use gh auth login or --token-file.")
        raw = result.stdout
    else:
        path = Path(path)
        if not path.is_absolute() or (os.name == "nt" and path.drive.startswith("\\\\")):
            raise SetupError("Supply an absolute local --token-file path.")
        try:
            record = path.lstat()
            if (
                not stat.S_ISREG(record.st_mode)
                or path.is_symlink()
                or getattr(record, "st_file_attributes", 0) & 0x400
            ):
                raise SetupError("Use an ordinary local credential file.")
            if os.name == "posix" and (record.st_uid != os.getuid() or record.st_mode & 0o077):
                raise SetupError("Use an owner-only credential file (chmod 600).")
            with path.open("rb") as handle:
                raw = handle.read(4099)
        except OSError:
            raise SetupError("Could not open the credential file.") from None
    token = raw.rstrip(b"\r\n")
    if len(raw) > 4098 or not re.fullmatch(rb"[A-Za-z0-9_.-]{1,4096}", token):
        raise SetupError("The credential should be a single GitHub access token.")
    return token.decode("ascii")


class GitHub:
    def __init__(self, token):
        self._token = token

    def request(self, method, path, payload=None):
        # This tool uses one fixed HTTPS origin and never follows a redirect.
        connection = http.client.HTTPSConnection("api.github.com", timeout=20)
        headers = {
            "User-Agent": "xdev-mail-setup",
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self._token,
            "X-GitHub-Api-Version": API_VERSION,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(
                method, path, body=None if payload is None else json.dumps(payload).encode(), headers=headers
            )
            response = connection.getresponse()
            if response.status not in (200, 201):
                return response.status, None
            raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise SetupError("GitHub returned an oversized document.")
            return response.status, json.loads(raw)
        except (OSError, http.client.HTTPException, ValueError):
            raise SetupError(f"GitHub {method} was not confirmed. Check the same repository with doctor.") from None
        finally:
            connection.close()


def fetch(api, path, missing=False):
    status, doc = api.request("GET", path)
    if missing and status == 404:
        return None
    if status != 200 or not isinstance(doc, (dict, list)):
        raise SetupError(f"GitHub lookup failed (HTTP {status}). Check login, access and repository name.")
    return doc


def create(api, path, payload):
    status, doc = api.request("POST", path, payload)
    # A concurrent creator may have won; the caller reads back every marker and ID.
    if status == 422:
        return None
    if status != 201 or not isinstance(doc, dict):
        raise SetupError(f"GitHub creation was not confirmed (HTTP {status}). Check doctor before retrying.")
    return positive(doc.get("id"))


def positive(value):
    if type(value) is not int or value < 1:
        raise SetupError("GitHub returned an invalid object ID.")
    return value


def repo_record(doc, name, expected=None):
    if not isinstance(doc, dict) or str(doc.get("full_name", "")).lower() != name.lower():
        raise SetupError("The repository identity did not match the selected owner/name.")
    ident = positive(doc.get("id"))
    if expected is not None and ident != expected:
        raise SetupError("The repository numeric ID changed.")
    if doc.get("private") is not True or doc.get("visibility") != "private":
        raise SetupError("Select a private repository.")
    if doc.get("archived") or doc.get("disabled") or doc.get("fork") or doc.get("description") != DESCRIPTION:
        raise SetupError("This repository is not an active dedicated xdev mailbox. Choose an unused name.")
    return ident


def inspect_release(doc, repo_id, expected=None):
    marker = f"<!-- xdev.github-mail.v1 repository_id={repo_id} -->"
    if (
        not isinstance(doc, dict)
        or doc.get("tag_name") != TAG
        or doc.get("prerelease") is not True
        or doc.get("draft") is not False
        or doc.get("immutable") is not False
        or marker not in str(doc.get("body", "")).splitlines()
    ):
        raise SetupError("The release must be the dedicated, published and mutable xdev mailbox prerelease.")
    ident = positive(doc.get("id"))
    if expected is not None and ident != expected:
        raise SetupError("The release numeric ID changed.")
    return ident


def setup(api, mode, name, repo_id=None, release_id=None):
    if (
        mode not in ("init", "doctor")
        or not isinstance(name, str)
        or not REPO.fullmatch(name)
        or name.split("/")[1] in (".", "..")
    ):
        raise SetupError("Specify init or doctor and the exact GitHub owner/repository.")
    if mode == "init" and (repo_id is not None or release_id is not None):
        raise SetupError("Use doctor to compare previously recorded numeric IDs.")
    owner, repo = name.split("/")
    base = "/repos/" + name
    document = fetch(api, base, missing=True)
    if document is None:
        if mode == "doctor":
            raise SetupError("Repository not visible. Check its name and access, or use init to create it.")
        user = fetch(api, "/user")
        if not isinstance(user, dict) or not user.get("login"):
            raise SetupError("A signed-in user is required to initialize a repository.")
        if str(user["login"]).lower() == owner.lower():
            endpoint = "/user/repos"
        else:
            org = fetch(api, "/orgs/" + owner)
            if not isinstance(org, dict) or str(org.get("login", "")).lower() != owner.lower():
                raise SetupError("Select your own login or an organization where you can create a private repository.")
            endpoint = "/orgs/" + owner + "/repos"
        repo_id = create(
            api,
            endpoint,
            {
                "name": repo,
                "private": True,
                "auto_init": True,
                "description": DESCRIPTION,
                "has_issues": False,
                "has_wiki": False,
                "has_projects": False,
            },
        )
        document = fetch(api, base)
    repo_id = repo_record(document, name, repo_id)
    contents = fetch(api, base + "/contents")
    if not (
        isinstance(contents, list)
        and len(contents) == 1
        and isinstance(contents[0], dict)
        and contents[0].get("type") == "file"
        and contents[0].get("name") == "README.md"
    ):
        raise SetupError("Expected a dedicated repository with only its README.md at the root.")
    target = base + "/releases/tags/" + TAG
    release = fetch(api, target, missing=True)
    if release is None:
        if mode == "doctor":
            raise SetupError("The dedicated prerelease is missing. Run init on this mailbox.")
        policy, _ = api.request("GET", base + "/immutable-releases")
        if policy == 200:
            raise SetupError("Release immutability is enabled here. Choose a mailbox where release assets can change.")
        # Administration read is optional for setup; a contents-only credential
        # might not see this policy. The created release must still say immutable=false.
        if policy not in (403, 404):
            raise SetupError(f"Could not inspect release immutability (HTTP {policy}). Retry init for this name.")
        branch = document.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise SetupError("The initial README commit is not available yet. Retry init for this name.")
        head = fetch(api, base + "/commits/" + quote(branch, safe=""))
        sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", sha):
            raise SetupError("Could not verify the initial commit for the release tag.")
        # Do not adopt another project's tags or an existing collection of releases.
        if fetch(api, base + "/releases?per_page=1") != [] or fetch(api, base + "/tags?per_page=1") != []:
            raise SetupError("This repository already contains unrelated releases or tags. Choose an unused name.")
        repo_record(fetch(api, base), name, repo_id)
        release_id = create(
            api,
            base + "/releases",
            {
                "tag_name": TAG,
                "name": "xdev private mailbox",
                "target_commitish": sha,
                "body": f"Private mailbox for encrypted xdev release assets.\n\n<!-- xdev.github-mail.v1 repository_id={repo_id} -->",
                "draft": False,
                "prerelease": True,
                "make_latest": "false",
            },
        )
        release = fetch(api, target)
    release_id = inspect_release(release, repo_id, release_id)
    repo_record(fetch(api, base), name, repo_id)
    return {
        "ok": True,
        "repository": name.lower(),
        "repository_id": repo_id,
        "release_id": release_id,
        "private": True,
        "mutable_prerelease": True,
        "asset_write_verified": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("doctor", "init", "save-token"),
        help="inspect, explicitly create a mailbox, or locally save a scoped token",
    )
    parser.add_argument("repository", metavar="OWNER/REPO", nargs="?")
    parser.add_argument(
        "--token-file", metavar="ABSOLUTE_PATH", help="local credential file; defaults to the active gh login"
    )
    parser.add_argument("--repository-id", type=int, help="doctor: compare the expected numeric repository ID")
    parser.add_argument("--release-id", type=int, help="doctor: compare the expected numeric release ID")
    args = parser.parse_args(argv)
    try:
        if args.mode == "save-token":
            if args.repository is not None or args.repository_id is not None or args.release_id is not None:
                raise SetupError("save-token accepts only a new local --token-file.")
            result = save_token(args.token_file)
        else:
            if args.repository is None:
                raise SetupError("Supply the selected OWNER/REPO.")
            result = setup(
                GitHub(credential(args.token_file)), args.mode, args.repository, args.repository_id, args.release_id
            )
    except SetupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
