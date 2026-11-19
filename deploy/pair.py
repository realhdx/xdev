"""Stage identical xdev releases on two Windows hosts through OpenSSH."""

import argparse
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
from pathlib import PureWindowsPath
import re
import socket
import subprocess
import sys
import tempfile

import deploy


SSH_OPTIONS = [
    "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
    "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1",
    "-o", "ServerAliveInterval=4", "-o", "ServerAliveCountMax=2", "-o", "ForwardAgent=no",
]
STAGING_NAME = re.compile(r"xdev-deploy-[a-f0-9]{32}\Z")


def run(arguments, payload=None, timeout=90):
    try:
        completed = subprocess.run(
            arguments, input=payload, stdin=subprocess.DEVNULL if payload is None else None,
            capture_output=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{arguments[0]} timed out") from None
    stdout = completed.stdout.decode("utf-8", "replace").strip()
    stderr = completed.stderr.decode("utf-8", "replace").strip()
    if completed.returncode:
        raise RuntimeError(stdout or stderr or f"{arguments[0]} exited with {completed.returncode}")
    return stdout


def literal(text):
    return "'" + str(text).replace("'", "''") + "'"


def sftp_path(path, *, remote=False):
    name = str(path).replace("\\", "/")
    if any(char in name for char in '\r\n"'):
        raise ValueError("Release transfer path contains unsupported quote or newline")
    if remote and re.match(r"^[A-Za-z]:/", name):
        name = "/" + name
    return '"' + name + '"'


class Peer:
    def __init__(self, host, win_root):
        self.host = host
        self.root = win_root
        self.directory = None

    def powershell(self, body):
        script = (
            "$ErrorActionPreference='Stop'\n"
            "$ProgressPreference='SilentlyContinue'\n"
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)\n" + body
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return run([
            "ssh.exe", "-T", *SSH_OPTIONS, self.host, "powershell.exe",
            "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded
        ])

    @contextmanager
    def staging(self):
        info = json.loads(self.powershell(
            "$name = 'xdev-deploy-' + [Guid]::NewGuid().ToString('N')\n"
            "$folder = Join-Path ([IO.Path]::GetTempPath()) $name\n"
            "$entry = [IO.Directory]::CreateDirectory($folder)\n"
            "@{host=$env:COMPUTERNAME; directory=$entry.FullName} | ConvertTo-Json -Compress"
        ))
        directory = PureWindowsPath(info["directory"])
        if not directory.is_absolute() or not STAGING_NAME.fullmatch(directory.name):
            raise ValueError("Peer returned an invalid temporary staging path")
        self.directory = directory
        try:
            if info["host"].casefold() == socket.gethostname().casefold():
                raise ValueError("Peer alias resolved to this computer")
            yield
        finally:
            try:
                self.powershell(
                    f"$folder = {literal(directory)}\n"
                    "$item = Get-Item -LiteralPath $folder\n"
                    "$temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([char]92)\n"
                    "if ($item.Parent.FullName.TrimEnd([char]92) -ne $temp -or "
                    "$item.Name -notmatch '^xdev-deploy-[a-f0-9]{32}$' -or "
                    "($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) "
                    "{ throw 'Invalid deployment cleanup path' }\n"
                    "foreach($file in 'deploy.py','release.zip') {\n"
                    "  $path = Join-Path $folder $file\n"
                    "  if (Test-Path -LiteralPath $path -PathType Leaf) { Remove-Item -LiteralPath $path }\n"
                    "}\n"
                    "[IO.Directory]::Delete($folder, $false)"
                )
            except RuntimeError as error:
                print(f"Peer staging cleanup failed at {directory}: {error}", file=sys.stderr)
            self.directory = None

    def copy(self, script, archive=None):
        batch = f"put {sftp_path(script)} {sftp_path(self.directory / 'deploy.py', remote=True)}\n"
        if archive:
            batch += f"put {sftp_path(archive)} {sftp_path(self.directory / 'release.zip', remote=True)}\n"
        run(["sftp.exe", "-b", "-", *SSH_OPTIONS, self.host], batch.encode("utf-8"))

    def command(self, script_sha, *arguments):
        script = self.directory / "deploy.py"
        prefix = ["--root", self.root] if self.root else []
        argv = ",".join(map(literal, [script, *prefix, *arguments]))
        result = json.loads(self.powershell(
            f"$scriptFile = {literal(script)}\n"
            f"if ((Get-FileHash -LiteralPath $scriptFile -Algorithm SHA256).Hash -ne {literal(script_sha)}) "
            "{ throw 'Deployment script hash mismatch' }\n"
            "$python = (Get-Command python.exe -ErrorAction Stop).Source\n"
            f"$arguments = @({argv})\n"
            "& $python -I -B -X utf8 @arguments\n"
            "exit $LASTEXITCODE"
        ))
        if not result.pop("ok"):
            raise RuntimeError(result["error"])
        return result


def activate(root, peer, script_sha, version, local_stage, remote_stage):
    prior_local = local_stage["active"]
    prior_remote = remote_stage["active"]
    try:
        peer.command(script_sha, "activate", version, "--expect-current", prior_remote or "none")
        deploy.change_pointer(root, version, expect=prior_local or "none")
        local = deploy.status(root, verify=True)
        remote = peer.command(script_sha, "status", "--verify")
        if local["active"] != version or remote["active"] != version:
            raise RuntimeError("Both hosts did not report the expected active release")
        return local, remote
    except (ValueError, OSError, RuntimeError) as error:
        outcomes = []
        if prior_local != version:
            try:
                if deploy.status(root, verify=False)["active"] == version:
                    deploy.change_pointer(root, prior_local, expect=version)
                outcomes.append("local pointer checked")
            except (ValueError, OSError, RuntimeError) as recovery:
                outcomes.append(f"local rollback unverified: {recovery}")
        if prior_remote != version:
            try:
                state = peer.command(script_sha, "status")
                if state["active"] == version:
                    command = ["activate", prior_remote] if prior_remote else ["deactivate"]
                    peer.command(script_sha, *command, "--expect-current", version)
                outcomes.append("peer pointer checked")
            except (ValueError, OSError, RuntimeError) as recovery:
                outcomes.append(f"peer rollback unverified: {recovery}")
        raise RuntimeError(f"{error}. {', '.join(outcomes)}") from None


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--host", required=True, help="Existing peer SSH host alias, for example desktop")
    args.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    args.add_argument("--local-root", type=Path)
    args.add_argument("--remote-root")
    mode = args.add_mutually_exclusive_group()
    mode.add_argument("--stage-only", action="store_true")
    mode.add_argument("--activate-staged", "--rollback", dest="staged", type=deploy.release_id, metavar="RELEASE",
                      help="Activate or roll back to an identical release retained by both hosts")
    options = args.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", options.host):
        raise ValueError("Expected a single SSH host alias")
    root = (options.local_root or deploy.install_root()).resolve()
    peer = Peer(options.host, options.remote_root)
    script = Path(deploy.__file__).resolve(strict=True)
    script_sha = deploy.digest(script)
    if options.staged:
        version = options.staged
        with peer.staging():
            peer.copy(script)
            local = deploy.status(root, verify=False)
            remote = peer.command(script_sha, "status")
            if version not in local["releases"] or version not in remote["releases"]:
                raise ValueError("The selected version must be present on both hosts")
            deploy.check_release(root / "releases" / version, version)
            local, remote = activate(root, peer, script_sha, version, local, remote)
            return {"release": version, "local": local, "peer": remote}
    with tempfile.TemporaryDirectory(prefix="xdev-release-") as tmp:
        archive = Path(tmp) / "release.zip"
        bundle = deploy.build(options.source, archive)
        version = bundle["release"]
        with peer.staging():
            peer.copy(script, archive)
            local = deploy.stage(root, archive, version)
            remote = peer.command(
                script_sha, "stage", "--archive", peer.directory / "release.zip", "--sha256", version
            )
            if remote["release"] != version:
                raise RuntimeError("Peer receipt differs from the local release checksum")
            if not options.stage_only:
                local, remote = activate(root, peer, script_sha, version, local, remote)
            return {"release": version, "staged_only": options.stage_only, "local": local, "peer": remote}


if __name__ == "__main__":
    try:
        print(json.dumps({"ok": True, **main()}, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
