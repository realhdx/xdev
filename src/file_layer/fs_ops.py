"""Target-side filesystem operations (stat, ls, read, mkdir, create, write, edit, copy, move, remove)."""

from __future__ import annotations

import base64
import contextlib
import fnmatch
import hashlib
import os
import pathlib
import shutil
from typing import Any, Optional

from .common import (
    DEFAULT_AUTO_HASH_MAX_BYTES,
    ERR_ALREADY_EXISTS,
    ERR_BASE_SHA256_MISMATCH,
    ERR_DIRECTORY_NOT_EMPTY,
    ERR_EDIT_PATTERN_NOT_FOUND,
    ERR_INVALID_ARGUMENT,
    ERR_IO_ERROR,
    ERR_NOT_A_DIRECTORY,
    ERR_NOT_A_FILE,
    ERR_PATH_NOT_FOUND,
    ERR_POST_TRANSFER_HASH_MISMATCH,
    CrossVolumeError,
    FileLayerError,
    TargetFileLock,
    atomic_replace_with_retry,
    canonicalize_win_path,
    compute_sha256,
    get_canonical_part_path,
    get_unique_temp_path,
    path_lock_key,
)


def ensure_not_reparse_or_root(path: pathlib.Path, abs_path: str, check_root: bool = False) -> None:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise FileLayerError(ERR_INVALID_ARGUMENT, f"Refusing to operate on symlink/junction: {abs_path}")
    if check_root and path.parent == path:
        raise FileLayerError(ERR_INVALID_ARGUMENT, f"Refusing to delete drive root: {abs_path}")


def check_base_sha256(
    target: pathlib.Path,
    abs_path: str,
    base_sha256: Optional[str],
    no_overwrite: bool = False,
) -> Optional[str]:
    ensure_not_reparse_or_root(target, abs_path)
    exists = target.exists()
    if exists and target.is_dir():
        raise FileLayerError(ERR_NOT_A_FILE, f"Target is a directory, not a file: {abs_path}")
    if no_overwrite and exists:
        raise FileLayerError(ERR_ALREADY_EXISTS, f"Target already exists: {abs_path}", {"canonical_path": abs_path})
    before_sha256 = compute_sha256(target) if exists else None
    if base_sha256 is not None:
        norm_expected = base_sha256.strip().lower()
        if norm_expected in ("none", "null", "empty"):
            if exists:
                raise FileLayerError(
                    ERR_BASE_SHA256_MISMATCH,
                    f"Expected target not to exist (base_sha256=NONE), but found file at {abs_path}",
                    {"expected_sha256": None, "actual_sha256": before_sha256, "canonical_path": abs_path},
                )
        elif before_sha256 != norm_expected:
            raise FileLayerError(
                ERR_BASE_SHA256_MISMATCH,
                f"Optimistic lock conflict on {abs_path}: expected {norm_expected}, actual {before_sha256}",
                {"expected_sha256": norm_expected, "actual_sha256": before_sha256, "canonical_path": abs_path},
            )
    return before_sha256


def op_stat(req: dict[str, Any]) -> dict[str, Any]:
    abs_path, norm_key, key_hash = path_lock_key(req["path"])
    target = pathlib.Path(abs_path)
    part_path = get_canonical_part_path(abs_path)
    part_exists = part_path.exists()
    part_size = part_path.stat().st_size if part_exists else 0

    if not target.exists():
        return {
            "exists": False,
            "is_dir": False,
            "is_file": False,
            "size": 0,
            "mtime_ns": None,
            "sha256": None,
            "prefix_sha256": None,
            "canonical_path": abs_path,
            "norm_key": norm_key,
            "lock_key": key_hash,
            "part_path": str(part_path),
            "part_exists": part_exists,
            "part_size": part_size,
        }

    st = target.stat()
    is_dir, is_file = target.is_dir(), target.is_file()
    with_sha256 = bool(req.get("with_sha256", False))
    no_sha256 = bool(req.get("no_sha256", False))
    prefix_bytes = req.get("prefix_bytes")

    sha256_val: Optional[str] = None
    skipped_large = False
    if is_file and not no_sha256:
        if with_sha256 or st.st_size <= DEFAULT_AUTO_HASH_MAX_BYTES:
            sha256_val = compute_sha256(target)
        else:
            skipped_large = True

    prefix_sha256 = compute_sha256(target, int(prefix_bytes)) if (is_file and prefix_bytes is not None and int(prefix_bytes) >= 0) else None
    return {
        "exists": True,
        "is_dir": is_dir,
        "is_file": is_file,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "sha256": sha256_val,
        "sha256_skipped_large_file": skipped_large,
        "prefix_sha256": prefix_sha256,
        "canonical_path": abs_path,
        "norm_key": norm_key,
        "lock_key": key_hash,
        "part_path": str(part_path),
        "part_exists": part_exists,
        "part_size": part_size,
    }


def op_ls(req: dict[str, Any]) -> dict[str, Any]:
    abs_path, _ = canonicalize_win_path(req["path"])
    target = pathlib.Path(abs_path)
    if not target.exists():
        raise FileLayerError(ERR_PATH_NOT_FOUND, f"Directory not found: {abs_path}", {"canonical_path": abs_path})
    if not target.is_dir():
        raise FileLayerError(ERR_NOT_A_DIRECTORY, f"Path is not a directory: {abs_path}", {"canonical_path": abs_path})

    recursive = bool(req.get("recursive", False))
    glob_pat = req.get("glob") or "*"
    limit = int(req.get("limit") or 1000)
    with_sha256 = bool(req.get("with_sha256", False))

    iterator = target.rglob("*") if recursive else target.iterdir()
    matched: list[tuple[str, pathlib.Path]] = []
    for p in iterator:
        if p.name.endswith(".xdev.part") or p.name.startswith(".xdev_tmp_"):
            continue
        rel = p.relative_to(target).as_posix()
        if glob_pat != "*" and not (fnmatch.fnmatch(p.name, glob_pat) or fnmatch.fnmatch(rel, glob_pat)):
            continue
        matched.append((rel, p))

    matched.sort(key=lambda item: item[0])
    entries: list[dict[str, Any]] = []
    for rel, p in matched[:limit]:
        st = p.stat()
        is_f = p.is_file()
        item: dict[str, Any] = {
            "name": p.name,
            "rel_path": rel,
            "path": str(p),
            "is_dir": p.is_dir(),
            "is_file": is_f,
            "size": st.st_size if is_f else 0,
            "mtime_ns": st.st_mtime_ns,
        }
        if with_sha256 and is_f:
            item["sha256"] = compute_sha256(p)
        entries.append(item)

    return {
        "canonical_path": abs_path,
        "count": len(entries),
        "total_matched": len(matched),
        "truncated": len(matched) > limit,
        "entries": entries,
    }


def op_read(req: dict[str, Any]) -> dict[str, Any]:
    abs_path, _ = canonicalize_win_path(req["path"])
    target = pathlib.Path(abs_path)
    if not target.exists():
        raise FileLayerError(ERR_PATH_NOT_FOUND, f"File not found: {abs_path}", {"canonical_path": abs_path})
    if not target.is_file():
        raise FileLayerError(ERR_NOT_A_FILE, f"Path is not a file: {abs_path}", {"canonical_path": abs_path})

    st = target.stat()
    offset_bytes, max_bytes = req.get("offset_bytes"), req.get("max_bytes")
    start_line, end_line = req.get("start_line"), req.get("end_line")
    encoding = (req.get("encoding") or "utf-8").lower()
    with_sha256, no_sha256 = bool(req.get("with_sha256", False)), bool(req.get("no_sha256", False))

    is_range = any(v is not None for v in (offset_bytes, max_bytes, start_line, end_line))
    if start_line is not None or end_line is not None:
        s_line = max(1, int(start_line or 1))
        e_line = int(end_line) if end_line is not None else None
        selected: list[bytes] = []
        with open(target, "rb") as f:
            for idx, raw_line in enumerate(f, start=1):
                if idx < s_line:
                    continue
                if e_line is not None and idx > e_line:
                    break
                selected.append(raw_line)
        slice_bytes = b"".join(selected)
    elif offset_bytes is not None or max_bytes is not None:
        off = max(0, int(offset_bytes or 0))
        with open(target, "rb") as f:
            f.seek(off)
            slice_bytes = f.read(int(max_bytes)) if max_bytes is not None else f.read()
    else:
        slice_bytes = target.read_bytes()

    range_sha256 = hashlib.sha256(slice_bytes).hexdigest()
    file_sha256: Optional[str] = None
    if not no_sha256:
        if not is_range:
            if with_sha256 or st.st_size <= DEFAULT_AUTO_HASH_MAX_BYTES:
                file_sha256 = range_sha256 if len(slice_bytes) == st.st_size else compute_sha256(target)
        elif with_sha256:
            file_sha256 = compute_sha256(target)

    out: dict[str, Any] = {
        "canonical_path": abs_path,
        "file_size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "bytes_read": len(slice_bytes),
        "is_range": is_range,
        "range_sha256": range_sha256,
        "file_sha256": file_sha256,
        "encoding": encoding,
    }
    if encoding == "base64":
        out["content_b64"] = base64.b64encode(slice_bytes).decode("ascii")
    else:
        out["content"] = slice_bytes.decode("utf-8", errors="replace")
    return out


def op_mkdir(req: dict[str, Any]) -> dict[str, Any]:
    abs_path, _ = canonicalize_win_path(req["path"])
    target = pathlib.Path(abs_path)
    ensure_not_reparse_or_root(target, abs_path)
    parents, exist_ok = bool(req.get("parents", True)), bool(req.get("exist_ok", True))
    if target.exists() and not exist_ok:
        raise FileLayerError(ERR_ALREADY_EXISTS, f"Path already exists: {abs_path}", {"canonical_path": abs_path})
    target.mkdir(parents=parents, exist_ok=exist_ok)
    return {"canonical_path": abs_path, "created": True}


def op_write_or_create(req: dict[str, Any], create_only: bool = False) -> dict[str, Any]:
    abs_path, _, key_hash = path_lock_key(req["path"])
    target = pathlib.Path(abs_path)
    lock_timeout = float(req.get("lock_timeout_sec") or 10.0)
    raw_bytes = base64.b64decode(req["content_b64"]) if "content_b64" in req else (req.get("content") or "").encode("utf-8")

    with TargetFileLock(abs_path, timeout_sec=lock_timeout):
        if bool(req.get("parents", True)):
            target.parent.mkdir(parents=True, exist_ok=True)
        elif not target.parent.exists():
            raise FileLayerError(ERR_PATH_NOT_FOUND, f"Parent directory does not exist: {target.parent}")
        if bool(req.get("no_create", False)) and not target.exists():
            raise FileLayerError(ERR_PATH_NOT_FOUND, f"Target file does not exist (--no-create): {abs_path}")

        before_sha256 = check_base_sha256(target, abs_path, req.get("base_sha256"), no_overwrite=create_only)
        expected_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        tmp_path = get_unique_temp_path(abs_path)
        try:
            with open(tmp_path, "wb") as f:
                f.write(raw_bytes)
                f.flush()
                os.fsync(f.fileno())
            written_sha256 = compute_sha256(tmp_path)
            if written_sha256 != expected_sha256:
                raise FileLayerError(ERR_POST_TRANSFER_HASH_MISMATCH, f"Staging write hash mismatch on {abs_path}")
            atomic_replace_with_retry(tmp_path, target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        st = target.stat()
        return {
            "canonical_path": abs_path,
            "lock_key": key_hash,
            "before_sha256": before_sha256,
            "after_sha256": written_sha256,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        }


def op_edit(req: dict[str, Any]) -> dict[str, Any]:
    abs_path, _, key_hash = path_lock_key(req["path"])
    target = pathlib.Path(abs_path)
    lock_timeout = float(req.get("lock_timeout_sec") or 10.0)
    base_sha256 = req.get("base_sha256")
    if not base_sha256 and not bool(req.get("allow_unlocked", False)):
        raise FileLayerError(ERR_INVALID_ARGUMENT, "edit requires --base-sha256 (or --allow-unlocked)")
    ops = req.get("ops") or []
    if not ops:
        raise FileLayerError(ERR_INVALID_ARGUMENT, "edit requires non-empty ops list")

    with TargetFileLock(abs_path, timeout_sec=lock_timeout):
        if not target.exists():
            raise FileLayerError(ERR_PATH_NOT_FOUND, f"File not found: {abs_path}", {"canonical_path": abs_path})
        before_sha256 = check_base_sha256(target, abs_path, base_sha256)
        raw_text = target.read_bytes().decode("utf-8")
        has_crlf = "\r\n" in raw_text
        updated = raw_text
        for idx, item in enumerate(ops):
            old_s, new_s, count = item.get("old"), item.get("new", ""), int(item.get("count", 1))
            if not old_s:
                raise FileLayerError(ERR_INVALID_ARGUMENT, f"Edit op #{idx} has empty 'old' pattern")
            cand_old, cand_new = old_s, new_s
            if cand_old not in updated and has_crlf and "\r\n" not in old_s and "\n" in old_s:
                cand_old = old_s.replace("\n", "\r\n")
                cand_new = new_s.replace("\r\n", "\n").replace("\n", "\r\n")
            if cand_old not in updated:
                raise FileLayerError(ERR_EDIT_PATTERN_NOT_FOUND, f"Edit op #{idx} pattern not found in {abs_path}", {"op_index": idx})
            updated = updated.replace(cand_old, cand_new, count) if count > 0 else updated.replace(cand_old, cand_new)

        raw_out = updated.encode("utf-8")
        expected_sha256 = hashlib.sha256(raw_out).hexdigest()
        tmp_path = get_unique_temp_path(abs_path)
        try:
            with open(tmp_path, "wb") as f:
                f.write(raw_out)
                f.flush()
                os.fsync(f.fileno())
            written_sha256 = compute_sha256(tmp_path)
            if written_sha256 != expected_sha256:
                raise FileLayerError(ERR_POST_TRANSFER_HASH_MISMATCH, "Staging edit hash mismatch")
            atomic_replace_with_retry(tmp_path, target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        st = target.stat()
        return {
            "canonical_path": abs_path,
            "lock_key": key_hash,
            "before_sha256": before_sha256,
            "after_sha256": written_sha256,
            "replacements_applied": len(ops),
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        }


def op_copy(req: dict[str, Any]) -> dict[str, Any]:
    src_abs, _ = canonicalize_win_path(req["src"])
    dst_abs, _, dst_key = path_lock_key(req["dst"])
    src_p, dst_p = pathlib.Path(src_abs), pathlib.Path(dst_abs)
    ensure_not_reparse_or_root(src_p, src_abs)
    if not src_p.exists():
        raise FileLayerError(ERR_PATH_NOT_FOUND, f"Source not found: {src_abs}", {"src_path": src_abs})
    if not src_p.is_file():
        raise FileLayerError(ERR_NOT_A_FILE, f"Source is not a file: {src_abs}", {"src_path": src_abs})

    with TargetFileLock(dst_abs, timeout_sec=float(req.get("lock_timeout_sec") or 10.0)):
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        before_sha256 = check_base_sha256(dst_p, dst_abs, req.get("base_sha256"), no_overwrite=bool(req.get("no_overwrite", False)))
        src_sha256 = compute_sha256(src_p)
        tmp_path = get_unique_temp_path(dst_abs)
        try:
            shutil.copy2(src_p, tmp_path)
            if compute_sha256(tmp_path) != src_sha256:
                raise FileLayerError(ERR_POST_TRANSFER_HASH_MISMATCH, "Copy verification hash mismatch")
            atomic_replace_with_retry(tmp_path, dst_p)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        st = dst_p.stat()
        return {
            "src_canonical_path": src_abs,
            "canonical_path": dst_abs,
            "lock_key": dst_key,
            "before_sha256": before_sha256,
            "after_sha256": src_sha256,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        }


def op_move(req: dict[str, Any]) -> dict[str, Any]:
    src_abs, src_norm, src_key = path_lock_key(req["src"])
    dst_abs, dst_norm, dst_key = path_lock_key(req["dst"])
    src_p, dst_p = pathlib.Path(src_abs), pathlib.Path(dst_abs)
    lock_timeout = float(req.get("lock_timeout_sec") or 10.0)
    locks = [TargetFileLock(src_abs, lock_timeout)] if src_norm == dst_norm else [
        TargetFileLock(p, lock_timeout) for _, p in sorted([(src_key, src_abs), (dst_key, dst_abs)])
    ]
    with contextlib.ExitStack() as stack:
        for lk in locks:
            stack.enter_context(lk)
        ensure_not_reparse_or_root(src_p, src_abs, check_root=True)
        if not src_p.exists():
            raise FileLayerError(ERR_PATH_NOT_FOUND, f"Source not found: {src_abs}", {"src_path": src_abs})
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        before_sha256 = check_base_sha256(dst_p, dst_abs, req.get("base_sha256"), no_overwrite=bool(req.get("no_overwrite", False)))
        is_file = src_p.is_file()
        src_sha256 = compute_sha256(src_p) if is_file else None
        if src_norm != dst_norm:
            try:
                atomic_replace_with_retry(src_p, dst_p)
            except CrossVolumeError:
                if not is_file:
                    raise FileLayerError(ERR_IO_ERROR, f"Cross-volume directory move not supported: {src_abs} -> {dst_abs}")
                tmp_path = get_unique_temp_path(dst_abs)
                try:
                    shutil.copy2(src_p, tmp_path)
                    if compute_sha256(tmp_path) != src_sha256:
                        raise FileLayerError(ERR_POST_TRANSFER_HASH_MISMATCH, "Cross-volume move hash mismatch")
                    atomic_replace_with_retry(tmp_path, dst_p)
                    src_p.unlink()
                finally:
                    if tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
        st = dst_p.stat()
        return {
            "src_canonical_path": src_abs,
            "canonical_path": dst_abs,
            "before_sha256": before_sha256,
            "after_sha256": compute_sha256(dst_p) if is_file else None,
            "size": st.st_size if is_file else 0,
            "mtime_ns": st.st_mtime_ns,
        }


def op_remove(req: dict[str, Any]) -> dict[str, Any]:
    abs_path, _, key_hash = path_lock_key(req["path"])
    target = pathlib.Path(abs_path)
    with TargetFileLock(abs_path, timeout_sec=float(req.get("lock_timeout_sec") or 10.0)):
        if not target.exists() and not target.is_symlink():
            if bool(req.get("missing_ok", False)):
                return {"canonical_path": abs_path, "removed": False, "before_sha256": None}
            raise FileLayerError(ERR_PATH_NOT_FOUND, f"Path not found: {abs_path}", {"canonical_path": abs_path})
        ensure_not_reparse_or_root(target, abs_path, check_root=True)
        resolved = str(target.resolve())
        if os.path.normcase(resolved) != os.path.normcase(abs_path):
            raise FileLayerError(ERR_INVALID_ARGUMENT, f"Refusing to remove path whose resolved target differs: {abs_path} -> {resolved}")
        if target.is_dir():
            if req.get("base_sha256"):
                raise FileLayerError(ERR_NOT_A_FILE, f"Cannot check base_sha256 on directory: {abs_path}")
            try:
                target.rmdir()
            except OSError as e:
                raise FileLayerError(
                    ERR_DIRECTORY_NOT_EMPTY,
                    f"Refusing to delete non-empty directory (recursive removal disabled in v1): {abs_path} ({e})",
                    {"canonical_path": abs_path},
                )
            return {"canonical_path": abs_path, "removed": True, "is_dir": True, "before_sha256": None}
        before_sha256 = check_base_sha256(target, abs_path, req.get("base_sha256"))
        target.unlink()
        return {"canonical_path": abs_path, "lock_key": key_hash, "removed": True, "is_dir": False, "before_sha256": before_sha256}
