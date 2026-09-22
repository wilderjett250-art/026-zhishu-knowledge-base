"""Bounded local import of explicitly selected existing catalog items."""

from pathlib import Path

from pkas.codex_capture import redact_secrets
from pkas.config import Settings
from pkas.db import Database
from pkas.ingest import SKIP_DIRECTORIES, IngestionService, is_sensitive_path, sha256_file
from pkas.parsers import SUPPORTED_EXTENSIONS, parse_file
from pkas.repository import Repository, utc_now


def _is_link_or_junction(path: Path) -> bool:
    """Use the Windows-only junction check when the current Python supports it."""
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(callable(is_junction) and is_junction())


def import_catalog_items(settings: Settings, item_ids: list[str], *, confirmed: bool) -> list[dict]:
    if not confirmed or not 1 <= len(item_ids) <= 10:
        raise ValueError("必须明确确认1至10份既有台账文件")
    settings = settings.model_copy(
        update={
            "document_ai_enhancement_enabled": False,
            "document_docling_enabled": False,
            "document_paddleocr_enabled": False,
            "document_allow_remote_processing": False,
            "document_allow_restricted_remote_processing": False,
        }
    )
    db = Database(settings)
    repo = Repository(db)
    ingestion = IngestionService(settings, repo)
    selected = []
    with db.connect() as c:
        for item_id in dict.fromkeys(item_ids):
            row = c.execute(
                "SELECT i.*,r.root_uri,r.enabled,r.connector_type,r.domain,r.privacy "
                "FROM sync_items i JOIN sync_roots r ON r.id=i.root_id WHERE i.id=?",
                (item_id,),
            ).fetchone()
            if not row or not row["enabled"] or row["connector_type"] != "local_files":
                raise ValueError("文件不在已启用的本地资料源中")
            raw = Path(row["source_uri"])
            root = Path(row["root_uri"]).resolve(strict=True)
            path = raw.resolve(strict=True)
            if root not in path.parents or not path.is_file():
                raise ValueError("文件越过授权目录边界")
            if any(
                _is_link_or_junction(p)
                for p in [raw, *raw.parents]
                if p != root and root in p.parents
            ):
                raise ValueError("不通过链接导入文件")
            if is_sensitive_path(path) or any(p.casefold() in SKIP_DIRECTORIES for p in path.parts):
                raise ValueError("文件命中排除规则")
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS or path.stat().st_size > 10_000_000:
                raise ValueError("本次只处理受支持且小于10MB的文件")
            selected.append((dict(row), path))
    results = []
    for row, path in selected:
        before = sha256_file(path)
        parsed = parse_file(path, settings=settings, privacy=row["privacy"])
        _, redactions = redact_secrets(parsed.text)
        if redactions:
            raise ValueError("检测到疑似凭据，停止导入且不输出内容")
        result = ingestion.import_file(path, domain=row["domain"], privacy=row["privacy"])
        if sha256_file(path) != before:
            raise RuntimeError("原文件在导入期间发生变化，停止本批次")
        stat = path.stat()
        with db.connect() as c:
            c.execute(
                "UPDATE sync_items SET state='indexed',reason=NULL,source_id=?,"
                "byte_size=?,modified_ns=?,fingerprint=?,indexed_at=? WHERE id=?",
                (
                    result["source_id"],
                    stat.st_size,
                    stat.st_mtime_ns,
                    f"{stat.st_size}:{stat.st_mtime_ns}",
                    utc_now(),
                    row["id"],
                ),
            )
            c.commit()
        results.append(
            {
                "item_id": row["id"],
                "source_id": result["source_id"],
                "status": result["status"],
                "source_hash": before,
                "original_unchanged": True,
                "chunks": result.get("chunk_count"),
                "cloud_called": False,
            }
        )
    return results
