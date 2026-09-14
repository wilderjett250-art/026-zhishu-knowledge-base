from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pkas.config import Settings
from pkas.local_lock import WindowsFileLock

BACKUP_PROTOCOL = "pkas-recovery-bundle-v1"
CRITICAL_TABLES = (
    "sources",
    "documents",
    "blocks",
    "chunks",
    "chunks_fts",
    "sync_roots",
    "agent_jobs",
    "index_outbox",
    "rag_eval_cases",
    "rag_eval_judgments",
)


class BackupError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _database_invariants(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }
    counts = {
        table: int(connection.execute(f'SELECT COUNT(1) FROM "{table}"').fetchone()[0])
        for table in CRITICAL_TABLES
        if table in tables
    }
    schema_row = connection.execute(
        "SELECT value FROM app_meta WHERE key='schema_version'"
    ).fetchone()
    return {
        "schema_version": int(schema_row[0]) if schema_row else 0,
        "counts": counts,
    }


def _backup_sqlite(
    source_path: Path,
    target_path: Path,
    *,
    include_pkas_invariants: bool = True,
) -> dict[str, Any]:
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target, pages=32768, sleep=0.05)
        target.commit()
        quick_check = str(target.execute("PRAGMA quick_check").fetchone()[0])
        invariants = _database_invariants(target) if include_pkas_invariants else None
    finally:
        target.close()
        source.close()
    if quick_check != "ok":
        raise BackupError("SQLite snapshot quick_check failed.")
    return {
        "path": target_path.name,
        "bytes": target_path.stat().st_size,
        "sha256": sha256_file(target_path),
        "quick_check": quick_check,
        "invariants": invariants,
    }


def _copy_raw_tree(source_root: Path, target_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not source_root.is_dir():
        return records
    for source in sorted(source_root.rglob("*")):
        if source.is_symlink():
            raise BackupError("Raw vault contains a symbolic link; backup stopped.")
        if not source.is_file():
            continue
        relative = source.relative_to(source_root)
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        records.append(
            {
                "path": relative.as_posix(),
                "bytes": target.stat().st_size,
                "sha256": sha256_file(target),
            }
        )
    return records


@dataclass(slots=True)
class BackupManager:
    settings: Settings

    @property
    def root(self) -> Path:
        return self.settings.data_root / "backups" / "scheduled"

    def create_bundle(
        self,
        *,
        label: str = "scheduled",
        retention: int = 3,
        output_root: Path | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", label):
            raise BackupError("Backup label must use lowercase letters, numbers, and hyphens.")
        if retention < 1 or retention > 30:
            raise BackupError("Backup retention must be between 1 and 30.")
        database_path = self.settings.database_path
        if not database_path.is_file():
            raise BackupError("PKAS database does not exist.")
        root = (output_root or self.root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(root).free
        raw_bytes = sum(
            path.stat().st_size
            for path in (self.settings.data_root / "raw").rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        required_bytes = database_path.stat().st_size + raw_bytes + 512 * 1024 * 1024
        if free_bytes < required_bytes:
            raise BackupError("Insufficient free space for a verified recovery bundle.")

        lock = WindowsFileLock(self.settings.data_root / "runs" / "backup.lock")
        if not lock.acquire():
            return {"status": "already_running", "protocol": BACKUP_PROTOCOL}
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        final = root / f"pkas-{label}-{timestamp}"
        staging = root / f".partial-{uuid.uuid4().hex}"
        try:
            staging.mkdir(parents=True)
            database_record = _backup_sqlite(database_path, staging / "pkas.sqlite")
            checkpoint_record = None
            if self.settings.agent_checkpoint_path.is_file():
                checkpoint_record = _backup_sqlite(
                    self.settings.agent_checkpoint_path,
                    staging / "langgraph-checkpoints.sqlite",
                    include_pkas_invariants=False,
                )
            raw_records = _copy_raw_tree(
                self.settings.data_root / "raw", staging / "raw"
            )
            manifest: dict[str, Any] = {
                "status": "completed",
                "protocol": BACKUP_PROTOCOL,
                "created_at": datetime.now(UTC).isoformat(),
                "label": label,
                "database": database_record,
                "agent_checkpoint": checkpoint_record,
                "raw": {
                    "file_count": len(raw_records),
                    "bytes": sum(int(item["bytes"]) for item in raw_records),
                    "files": raw_records,
                },
                "excluded": {
                    "secrets": True,
                    "qdrant": "rebuild_required",
                    "runs": True,
                    "existing_backups": True,
                },
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            staging.replace(final)
            bundles = sorted(
                (
                    path
                    for path in root.glob(f"pkas-{label}-*")
                    if path.is_dir() and (path / "manifest.json").is_file()
                ),
                key=lambda path: path.name,
                reverse=True,
            )
            pruned: list[str] = []
            for expired in bundles[retention:]:
                shutil.rmtree(expired)
                pruned.append(expired.name)
            report = {
                "status": "completed",
                "protocol": BACKUP_PROTOCOL,
                "bundle": str(final),
                "manifest": str(final / "manifest.json"),
                "database_bytes": int(database_record["bytes"]),
                "raw_files": len(raw_records),
                "raw_bytes": manifest["raw"]["bytes"],
                "schema_version": database_record["invariants"]["schema_version"],
                "retention": retention,
                "pruned": pruned,
                "secrets_included": False,
                "qdrant_rebuild_required": True,
            }
            return report
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        finally:
            lock.release()

    def verify_bundle(self, bundle: Path) -> dict[str, Any]:
        resolved = bundle.resolve()
        manifest_path = resolved / "manifest.json"
        if not manifest_path.is_file():
            raise BackupError("Backup manifest is missing.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("protocol") != BACKUP_PROTOCOL:
            raise BackupError("Unsupported backup protocol.")
        database_path = resolved / str(manifest["database"]["path"])
        if not database_path.is_file():
            raise BackupError("Backup database is missing.")
        if sha256_file(database_path) != manifest["database"]["sha256"]:
            raise BackupError("Backup database SHA-256 mismatch.")
        with closing(sqlite3.connect(database_path)) as connection:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            invariants = _database_invariants(connection)
        if quick_check != "ok" or invariants != manifest["database"]["invariants"]:
            raise BackupError("Backup database verification failed.")
        checkpoint = manifest.get("agent_checkpoint")
        if checkpoint:
            checkpoint_path = resolved / str(checkpoint["path"])
            if (
                not checkpoint_path.is_file()
                or sha256_file(checkpoint_path) != checkpoint["sha256"]
            ):
                raise BackupError("Agent checkpoint verification failed.")
            with closing(sqlite3.connect(checkpoint_path)) as connection:
                checkpoint_quick_check = str(
                    connection.execute("PRAGMA quick_check").fetchone()[0]
                )
            if checkpoint_quick_check != "ok":
                raise BackupError("Agent checkpoint quick_check failed.")
        raw_root = resolved / "raw"
        raw_files = manifest["raw"]["files"]
        for record in raw_files:
            path = raw_root / str(record["path"])
            if (
                not path.is_file()
                or path.stat().st_size != int(record["bytes"])
                or sha256_file(path) != record["sha256"]
            ):
                raise BackupError("Raw vault verification failed.")
        return {
            "status": "verified",
            "protocol": BACKUP_PROTOCOL,
            "bundle": str(resolved),
            "schema_version": invariants["schema_version"],
            "database_sha256": manifest["database"]["sha256"],
            "raw_files": len(raw_files),
            "raw_bytes": manifest["raw"]["bytes"],
            "secrets_included": False,
        }

    def restore_bundle(
        self,
        bundle: Path,
        target_data_root: Path,
        *,
        disable_sync_roots: bool = True,
    ) -> dict[str, Any]:
        target = target_data_root.resolve()
        if target == self.settings.data_root.resolve():
            raise BackupError("Refusing to restore over the active PKAS data root.")
        if target.exists() and any(target.iterdir()):
            raise BackupError("Restore target must be absent or empty.")
        verification = self.verify_bundle(bundle)
        resolved_bundle = bundle.resolve()
        manifest = json.loads(
            (resolved_bundle / "manifest.json").read_text(encoding="utf-8")
        )
        staging = target.parent / f".partial-restore-{uuid.uuid4().hex}"
        try:
            (staging / "index").mkdir(parents=True)
            shutil.copy2(
                resolved_bundle / "pkas.sqlite", staging / "index" / "pkas.sqlite"
            )
            if (resolved_bundle / "langgraph-checkpoints.sqlite").is_file():
                shutil.copy2(
                    resolved_bundle / "langgraph-checkpoints.sqlite",
                    staging / "index" / "langgraph-checkpoints.sqlite",
                )
            shutil.copytree(resolved_bundle / "raw", staging / "raw")

            database_path = staging / "index" / "pkas.sqlite"
            with closing(sqlite3.connect(database_path)) as connection:
                remapped = 0
                missing_vault_files = 0
                for table in ("sources", "connector_snapshots"):
                    rows = connection.execute(
                        f'SELECT id, vault_path FROM "{table}"'
                    ).fetchall()
                    for record_id, vault_path in rows:
                        parts = Path(str(vault_path)).parts
                        lower = [part.lower() for part in parts]
                        if "raw" not in lower:
                            missing_vault_files += 1
                            continue
                        raw_index = len(lower) - 1 - lower[::-1].index("raw")
                        relative = Path(*parts[raw_index + 1 :])
                        restored_path = target / "raw" / relative
                        staged_path = staging / "raw" / relative
                        if not staged_path.is_file():
                            missing_vault_files += 1
                            continue
                        connection.execute(
                            f'UPDATE "{table}" SET vault_path=? WHERE id=?',
                            (str(restored_path), record_id),
                        )
                        remapped += 1
                if disable_sync_roots:
                    connection.execute("UPDATE sync_roots SET enabled=0")
                connection.commit()
                quick_check = str(
                    connection.execute("PRAGMA quick_check").fetchone()[0]
                )
                invariants = _database_invariants(connection)
                fts_count = int(
                    connection.execute("SELECT COUNT(1) FROM chunks_fts").fetchone()[0]
                )
                chunk_count = int(
                    connection.execute("SELECT COUNT(1) FROM chunks").fetchone()[0]
                )
            if quick_check != "ok" or missing_vault_files:
                raise BackupError("Restored database or raw-vault verification failed.")
            if fts_count != chunk_count:
                raise BackupError("Restored FTS row count does not match chunks.")
            if target.exists():
                target.rmdir()
            staging.replace(target)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

        report = {
            "status": "restored",
            "protocol": BACKUP_PROTOCOL,
            "source_bundle": str(resolved_bundle),
            "target_data_root": str(target),
            "schema_version": invariants["schema_version"],
            "quick_check": quick_check,
            "vault_paths_remapped": remapped,
            "missing_vault_files": missing_vault_files,
            "chunks": chunk_count,
            "fts_rows": fts_count,
            "sync_roots_disabled": disable_sync_roots,
            "secrets_restored": False,
            "qdrant_rebuild_required": True,
            "verified_backup": verification["status"] == "verified",
            "manifest_database_sha256": manifest["database"]["sha256"],
        }
        report_path = target / "runs" / "restore-verification.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report["report_path"] = str(report_path)
        return report

    def status(self) -> dict[str, Any]:
        bundles = sorted(
            (
                path
                for path in self.root.glob("pkas-scheduled-*")
                if path.is_dir() and (path / "manifest.json").is_file()
            ),
            key=lambda path: path.name,
            reverse=True,
        ) if self.root.is_dir() else []
        latest = None
        if bundles:
            manifest = json.loads((bundles[0] / "manifest.json").read_text(encoding="utf-8"))
            latest = {
                "bundle": bundles[0].name,
                "created_at": manifest.get("created_at"),
                "schema_version": manifest["database"]["invariants"]["schema_version"],
                "database_bytes": manifest["database"]["bytes"],
                "raw_files": manifest["raw"]["file_count"],
                "raw_bytes": manifest["raw"]["bytes"],
            }
        return {
            "protocol": BACKUP_PROTOCOL,
            "bundle_count": len(bundles),
            "retention_target": 3,
            "latest": latest,
            "secrets_included": False,
            "qdrant_strategy": "rebuild",
        }
