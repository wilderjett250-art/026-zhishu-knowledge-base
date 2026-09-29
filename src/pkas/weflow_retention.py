"""Conservative retention for PKAS-owned WeFlow export *copies*.

The independently stored XLSX snapshot is never removed here.  Other WeFlow
exports, including the user's historical global export folder, are out of scope.
"""

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

KEEP_PER_SESSION = 2
MIN_AGE_MS = 7 * 24 * 60 * 60 * 1000
MANAGED_DIR_NAME = "PKAS-WeFlow-Exports"


def _reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _managed_root() -> Path | None:
    configured = os.environ.get("PKAS_WEFLOW_EXPORT_ROOT", "").strip()
    if not configured:
        return None
    raw = Path(configured)
    if not raw.is_absolute() or raw.name.casefold() != MANAGED_DIR_NAME.casefold():
        return None
    if not raw.is_dir() or _reparse_point(raw):
        return None
    return raw.resolve()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _record_file(record: dict[str, Any], root: Path) -> Path | None:
    raw = Path(str(record.get("outputPath") or ""))
    if not raw.is_absolute() or raw.suffix.casefold() != ".xlsx":
        return None
    try:
        if not raw.is_file() or _reparse_point(raw):
            return None
        # Do not follow a junction/symlink anywhere between root and the file.
        relative = raw.relative_to(root)
        if not relative.parts or any(part in (".", "..") for part in relative.parts):
            return None
        parent = raw.parent
        while parent != root:
            if _reparse_point(parent):
                return None
            parent = parent.parent
        resolved = raw.resolve()
        resolved.relative_to(root)
        return resolved
    except (OSError, ValueError):
        return None


def prune_verified_exports(
    *,
    records_path: Path,
    database_path: Path,
    data_root: Path,
    watermark_ms: int,
    now_ms: int,
) -> dict[str, int]:
    """Prune old managed XLSX only after verifying its DB and vault identity.

    Any missing proof leaves the file in place. The caller reports failures as
    a storage warning; import success and its watermark remain independent.
    """
    result = {"pruned_files": 0, "freed_bytes": 0, "retention_errors": 0}
    root = _managed_root()
    if root is None or watermark_ms <= 0:
        return result
    try:
        payload = json.loads(records_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or not database_path.is_file():
            result["retention_errors"] += 1
            return result
    except (OSError, ValueError):
        result["retention_errors"] += 1
        return result

    keep: set[Path] = set()
    candidates: dict[Path, int] = {}
    for records in payload.values():
        if not isinstance(records, list):
            result["retention_errors"] += 1
            return result
        managed: list[tuple[int, Path]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            path = _record_file(record, root)
            if path is None:
                continue
            try:
                export_ms = int(record.get("exportTime") or 0)
            except (ValueError, TypeError):
                continue
            managed.append((export_ms, path))
        managed.sort(key=lambda item: item[0], reverse=True)
        keep.update(path for _, path in managed[:KEEP_PER_SESSION])
        for export_ms, path in managed[KEEP_PER_SESSION:]:
            if 0 < export_ms <= watermark_ms and now_ms - export_ms >= MIN_AGE_MS:
                candidates[path] = max(candidates.get(path, 0), export_ms)

    vault_root = (data_root / "raw" / "weflow-xlsx" / "sha256").resolve()
    try:
        # URI mode prevents sqlite3 from creating an empty DB on a bad path.
        connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for path in sorted(candidates):
                if path in keep:
                    continue
                try:
                    if _record_file({"outputPath": str(path)}, root) != path:
                        continue
                    content_hash = _file_hash(path)
                    row = connection.execute(
                        "SELECT vault_path, byte_size FROM connector_snapshots "
                        "WHERE content_hash=?",
                        (content_hash,),
                    ).fetchone()
                    if row is None:
                        continue
                    vault = Path(str(row[0]))
                    if not vault.is_file() or _reparse_point(vault):
                        continue
                    vault.resolve().relative_to(vault_root)
                    size = path.stat().st_size
                    if int(row[1]) != size or vault.stat().st_size != size:
                        continue
                    if _file_hash(vault) != content_hash:
                        continue
                    path.unlink()
                    result["pruned_files"] += 1
                    result["freed_bytes"] += size
                except (OSError, ValueError, sqlite3.Error):
                    result["retention_errors"] += 1
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        result["retention_errors"] += 1
    return result
