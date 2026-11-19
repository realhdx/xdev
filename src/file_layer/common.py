"""Common utilities, Windows path canonicalization, locking, and hashing for xdev_file."""

from __future__ import annotations

import errno
import hashlib
import json
import msvcrt
import os
import pathlib
import tempfile
import time
from typing import Any, Optional

# Standard error codes per FILE_LAYER_CONTRACT.md
ERR_BASE_SHA256_MISMATCH = "BASE_SHA256_MISMATCH"
ERR_ALREADY_EXISTS = "ALREADY_EXISTS"
ERR_PATH_NOT_FOUND = "PATH_NOT_FOUND"
ERR_NOT_A_FILE = "NOT_A_FILE"
ERR_NOT_A_DIRECTORY = "NOT_A_DIRECTORY"
ERR_DIRECTORY_NOT_EMPTY = "DIRECTORY_NOT_EMPTY"
ERR_RESUME_PREFIX_MISMATCH = "RESUME_PREFIX_MISMATCH"
ERR_POST_TRANSFER_HASH_MISMATCH = "POST_TRANSFER_HASH_MISMATCH"
ERR_FILE_LOCK_TIMEOUT = "FILE_LOCK_TIMEOUT"
ERR_SSH_TRANSPORT_ERROR = "SSH_TRANSPORT_ERROR"
ERR_EDIT_PATTERN_NOT_FOUND = "EDIT_PATTERN_NOT_FOUND"
ERR_INVALID_ARGUMENT = "INVALID_ARGUMENT"
ERR_IO_ERROR = "IO_ERROR"

DEFAULT_AUTO_HASH_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB
CHUNK_SIZE = 1024 * 1024  # 1 MiB
SFTP_GLOB_CHARS = set("[]*?\"")


class CrossVolumeError(OSError):
    """Raised when os.replace fails specifically due to cross-volume move (EXDEV / WinError 17)."""


class FileLayerError(Exception):
    """Structured error carrying contract error_code and optional data payload."""

    def __init__(self, error_code: str, message: str, data: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.data = data or {}


def get_state_root() -> pathlib.Path:
    """
    Resolve the runtime state directory (<install_root>/state) respecting XDEV_HOME.
    Never creates an unmarked %LOCALAPPDATA%\\CrossDeviceCollab directory before deploy.py initializes it.
    """
    override = os.environ.get("XDEV_HOME")
    if override:
        state_dir = pathlib.Path(override) / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir

    local_app_data = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
    prod_root = pathlib.Path(local_app_data) / "CrossDeviceCollab"
    if (prod_root / ".xdev-install.json").is_file():
        state_dir = prod_root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir

    fallback_state = pathlib.Path(tempfile.gettempdir()) / "CrossDeviceCollab-dev" / "state"
    fallback_state.mkdir(parents=True, exist_ok=True)
    return fallback_state


def get_locks_dir() -> pathlib.Path:
    d = get_state_root() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_staging_dir() -> pathlib.Path:
    d = get_state_root() / "staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def canonicalize_win_path(raw_path: str) -> tuple[str, str]:
    """
    Canonicalize a Windows path semantically.
    Returns (display_abspath, normcase_key_path).
    """
    if not raw_path or not str(raw_path).strip():
        raise FileLayerError(ERR_INVALID_ARGUMENT, "Path must not be empty")
    expanded = os.path.expandvars(os.path.expanduser(str(raw_path)))
    abs_path = os.path.normpath(os.path.abspath(expanded))
    if len(abs_path) >= 2 and abs_path[1] == ":":
        abs_path = abs_path[0].upper() + abs_path[1:]
    norm_key = os.path.normcase(abs_path)
    return abs_path, norm_key


def path_lock_key(raw_path: str) -> tuple[str, str, str]:
    """Return (display_abspath, normcase_key_path, sha256_hex_of_normcase_key)."""
    abs_path, norm_key = canonicalize_win_path(raw_path)
    key_hash = hashlib.sha256(norm_key.encode("utf-8")).hexdigest()
    return abs_path, norm_key, key_hash


def has_sftp_glob_chars(path_str: str) -> bool:
    return any(ch in SFTP_GLOB_CHARS for ch in path_str)


def get_canonical_part_path(raw_target_path: str) -> pathlib.Path:
    """
    Return the deterministic resumable staging .xdev.part path for a target file.
    Protected by TargetFileLock(raw_target_path) so two concurrent writers targeting
    the same canonical Windows path never share or overwrite the .part file concurrently.
    If the target parent directory contains SFTP glob metacharacters ([ ] * ? "),
    places the .part file in <state_root>/staging/ so sftp never glob-expands brackets.
    """
    abs_path, _, key_hash = path_lock_key(raw_target_path)
    target = pathlib.Path(abs_path)
    part_name = f".xdev_part_{key_hash[:24]}.xdev.part"
    if has_sftp_glob_chars(str(target.parent)):
        return get_staging_dir() / part_name
    return target.parent / part_name


def get_unique_temp_path(raw_target_path: str) -> pathlib.Path:
    """Return a unique same-directory temporary file path for atomic write/edit/copy."""
    abs_path, _, key_hash = path_lock_key(raw_target_path)
    target = pathlib.Path(abs_path)
    nonce = f"{os.getpid()}_{time.time_ns()}"
    return target.parent / f".xdev_tmp_{key_hash[:12]}_{nonce}.tmp"


def to_sftp_remote_path(win_abs_path: str) -> str:
    """Convert Windows absolute path 'C:\\foo\\bar' to Windows OpenSSH SFTP path '/C:/foo/bar'."""
    posix_p = win_abs_path.replace("\\", "/")
    if len(posix_p) >= 2 and posix_p[1] == ":":
        return "/" + posix_p
    return posix_p


class TargetFileLock:
    """
    OS-level exclusive lock (msvcrt.locking) keyed by the canonical normalized Windows path.
    Held across the entire lifecycle of a write, edit, move, or SFTP upload/download + atomic commit.
    """

    def __init__(self, target_path: str, timeout_sec: float = 10.0) -> None:
        self.abs_path, self.norm_key, self.key_hash = path_lock_key(target_path)
        self.timeout_sec = max(0.0, float(timeout_sec))
        self.lock_file_path = get_locks_dir() / f"{self.key_hash}.lock"
        self._fh: Optional[Any] = None

    def acquire(self) -> "TargetFileLock":
        deadline = time.monotonic() + self.timeout_sec
        self.lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_file_path, "a+b")
        try:
            if fh.tell() == 0:
                fh.write(b"\x00")
                fh.flush()
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise FileLayerError(
                            ERR_FILE_LOCK_TIMEOUT,
                            f"Timed out waiting {self.timeout_sec:.2f}s for exclusive lock on {self.abs_path}",
                            {"canonical_path": self.abs_path, "lock_key": self.key_hash},
                        )
                    time.sleep(0.03)
            meta = json.dumps(
                {"pid": os.getpid(), "target": self.abs_path, "acquired_ns": time.time_ns()},
                ensure_ascii=False,
            ).encode("utf-8")
            fh.seek(1)
            fh.truncate(1)
            fh.write(meta)
            fh.flush()
            self._fh = fh
            return self
        except Exception:
            fh.close()
            raise

    def release(self) -> None:
        if self._fh is not None:
            fh = self._fh
            self._fh = None
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            finally:
                try:
                    fh.close()
                except OSError:
                    pass

    def __enter__(self) -> "TargetFileLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


def compute_sha256(path: pathlib.Path, max_bytes: Optional[int] = None) -> str:
    """Compute SHA-256 hex digest of a file (or its first max_bytes bytes)."""
    h = hashlib.sha256()
    remaining = max_bytes
    with open(path, "rb") as f:
        while True:
            read_len = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
            if read_len <= 0:
                break
            chunk = f.read(read_len)
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return h.hexdigest()


def atomic_replace_with_retry(
    src_temp: pathlib.Path,
    dst_target: pathlib.Path,
    max_attempts: int = 6,
) -> None:
    """
    Atomically replace dst_target with src_temp using os.replace, with short exponential
    backoff for transient Windows sharing locks. Raises CrossVolumeError on EXDEV / WinError 17.
    """
    delays = [0.02, 0.05, 0.1, 0.2, 0.35, 0.5]
    last_err: Optional[OSError] = None
    for attempt in range(max_attempts):
        try:
            os.replace(str(src_temp), str(dst_target))
            return
        except OSError as e:
            if e.errno == errno.EXDEV or getattr(e, "winerror", None) == 17:
                raise CrossVolumeError(str(e)) from e
            last_err = e
            if isinstance(e, PermissionError) and attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            break
    raise FileLayerError(
        ERR_IO_ERROR,
        f"Atomic replace failed for {dst_target}: {last_err}",
        {"src_temp": str(src_temp), "dst_target": str(dst_target)},
    )


def make_envelope(
    *,
    ok: bool,
    op: str,
    host: str,
    target_path: str,
    started_monotonic: float,
    data: Optional[dict[str, Any]] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> dict[str, Any]:
    elapsed_ms = int(round((time.monotonic() - started_monotonic) * 1000.0))
    return {
        "ok": bool(ok),
        "op": op,
        "host": host,
        "target_path": target_path,
        "error_code": error_code,
        "error_message": error_message,
        "elapsed_ms": elapsed_ms,
        "data": data or {},
    }
