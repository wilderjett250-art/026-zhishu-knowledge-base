import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pkas.config import Settings, get_settings
from pkas.document_extraction import DOCUMENT_PIPELINE_VERSION
from pkas.parsers import SUPPORTED_EXTENSIONS, ParsedBlock, ParsedDocument, ParseError, parse_file
from pkas.repository import Repository

SKIP_DIRECTORIES = {
    ".angular",
    ".cache",
    ".dart_tool",
    ".git",
    ".gradle",
    ".idea",
    ".mypy_cache",
    ".next",
    ".nuxt",
    ".pytest_cache",
    ".ruff_cache",
    ".terraform",
    ".tox",
    ".venv",
    ".vscode",
    ".yarn",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "obj",
    "out",
    "target",
    "vendor",
    "venv",
}
SENSITIVE_EXACT_NAMES = {
    ".env",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "secrets.json",
}
SENSITIVE_SUFFIXES = {
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
}


class ImportBoundaryError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_sensitive_path(path: Path) -> bool:
    lower_name = path.name.lower()
    if lower_name in SENSITIVE_EXACT_NAMES:
        return True
    return path.suffix.lower() in SENSITIVE_SUFFIXES


CHUNKER_VERSION = "typed-v2"
CODE_EXTENSIONS = {
    ".bat", ".c", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
    ".jsx", ".kt", ".ps1", ".py", ".rs", ".scss", ".sh", ".sql", ".svelte",
    ".swift", ".ts", ".tsx", ".vue",
}


def chunk_text(text: str, max_chars: int = 900, overlap: int = 90) -> list[dict[str, Any]]:
    clean = text.replace("\x00", "").strip()
    if not clean:
        return []

    paragraphs = [paragraph.strip() for paragraph in clean.splitlines() if paragraph.strip()]
    chunks: list[dict[str, Any]] = []
    buffer = ""
    start_paragraph = 0

    def append_chunk(content: str, start: int, end: int) -> None:
        chunks.append(
            {
                "sequence": len(chunks),
                "text": content.strip(),
                "locator": f"paragraph:{start}-{end}",
                "block_sequences": [],
                "chunk_kind": "normalized-text",
                "chunker_version": CHUNKER_VERSION,
            }
        )

    for index, paragraph in enumerate(paragraphs):
        if len(paragraph) > max_chars:
            if buffer:
                append_chunk(buffer, start_paragraph, index - 1)
                buffer = ""
            step = max_chars - overlap
            for offset in range(0, len(paragraph), step):
                segment = paragraph[offset : offset + max_chars]
                append_chunk(segment, index, index)
                if offset + max_chars >= len(paragraph):
                    break
            start_paragraph = index + 1
            continue

        candidate = f"{buffer}\n{paragraph}".strip() if buffer else paragraph
        if len(candidate) <= max_chars:
            if not buffer:
                start_paragraph = index
            buffer = candidate
            continue

        append_chunk(buffer, start_paragraph, index - 1)
        carry = buffer[-overlap:] if overlap and buffer else ""
        buffer = f"{carry}\n{paragraph}".strip()
        start_paragraph = index

    if buffer:
        append_chunk(buffer, start_paragraph, len(paragraphs) - 1)
    return chunks


def chunk_document(
    parsed: ParsedDocument,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> list[dict[str, Any]]:
    if not parsed.blocks:
        return chunk_text(
            parsed.text,
            max_chars=max_chars or 900,
            overlap=90 if overlap is None else overlap,
        )

    kinds = [block.kind for block in parsed.blocks]
    extension = str(parsed.metadata.get("source_extension") or "").lower()
    table_count = sum(kind in {"table", "table-row"} for kind in kinds)
    message_count = sum(kind == "message" for kind in kinds)
    if message_count >= max(1, len(kinds) // 2):
        profile = "message-window"
        target = max_chars or 1200
        max_units = 30
    elif table_count >= max(1, len(kinds) // 2):
        profile = "table-rows"
        target = max_chars or 1400
        max_units = 20
    elif extension in CODE_EXTENSIONS:
        profile = "code"
        target = max_chars or 1600
        max_units = 120
    else:
        profile = "document-section"
        target = max_chars or 900
        max_units = 80
    resolved_overlap = min(target // 4, 90 if overlap is None else max(0, overlap))

    chunks: list[dict[str, Any]] = []
    buffer: list[tuple[int, ParsedBlock]] = []
    buffer_chars = 0

    def append(content: str, locator: str, sequences: list[int]) -> None:
        chunks.append(
            {
                "sequence": len(chunks),
                "text": content.strip(),
                "locator": locator,
                "block_sequences": list(dict.fromkeys(sequences)),
                "chunk_kind": profile,
                "chunker_version": CHUNKER_VERSION,
            }
        )

    def flush() -> None:
        nonlocal buffer, buffer_chars
        if not buffer:
            return
        content = "\n\n".join(block.text.strip() for _, block in buffer if block.text.strip())
        if len(buffer) == 1:
            locator = buffer[0][1].locator
        else:
            locator = f"{buffer[0][1].locator}..{buffer[-1][1].locator}"
        append(content, locator, [sequence for sequence, _ in buffer])
        buffer = []
        buffer_chars = 0

    table_header: tuple[int, ParsedBlock] | None = None
    current_table_id: str | None = None
    section_heading: tuple[int, ParsedBlock] | None = None
    for block_sequence, block in enumerate(parsed.blocks):
        text = block.text.replace("\x00", "").strip()
        if not text:
            continue
        if block.kind == "heading":
            flush()
            section_heading = (block_sequence, block)
            table_header = None
            current_table_id = None
            buffer.append((block_sequence, block))
            buffer_chars = len(text)
            continue
        if profile == "table-rows" and block.kind in {"table", "table-row"}:
            derived_table_id = (
                block.locator.rsplit("/row:", 1)[0]
                if "/row:" in block.locator
                else block.locator.split(":", 1)[0]
            )
            table_id = str(
                block.metadata.get("table_id")
                or derived_table_id
            )
            if current_table_id is not None and table_id != current_table_id:
                flush()
                table_header = None
            current_table_id = table_id
            if table_header is None or bool(block.metadata.get("is_header")):
                table_header = (block_sequence, block)
            if not buffer and section_heading:
                buffer.append(section_heading)
                buffer_chars += len(section_heading[1].text)
            if not buffer and table_header[0] != block_sequence:
                buffer.append(table_header)
                buffer_chars += len(table_header[1].text)
        if len(text) > target:
            flush()
            step = max(1, target - resolved_overlap)
            for offset in range(0, len(text), step):
                segment = text[offset : offset + target]
                append(
                    segment,
                    f"{block.locator}/chars:{offset}-{offset + len(segment)}",
                    [block_sequence],
                )
                if offset + target >= len(text):
                    break
            continue

        candidate_chars = buffer_chars + len(text) + (2 if buffer else 0)
        if buffer and (candidate_chars > target or len(buffer) >= max_units):
            flush()
            if profile == "table-rows":
                if section_heading:
                    buffer.append(section_heading)
                    buffer_chars += len(section_heading[1].text)
                if table_header and table_header[0] != block_sequence:
                    buffer.append(table_header)
                    buffer_chars += len(table_header[1].text)
        buffer.append((block_sequence, block))
        buffer_chars += len(text) + (2 if len(buffer) > 1 else 0)

    flush()
    return chunks


class IngestionService:
    def __init__(
        self,
        settings: Settings | None = None,
        repository: Repository | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository or Repository()

    def _validate_target(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise ImportBoundaryError("导入路径必须是绝对路径。")
        resolved = path.resolve(strict=True)
        if resolved == Path(resolved.anchor):
            raise ImportBoundaryError("不允许直接导入整个磁盘根目录。")
        project_root = self.settings.project_root.resolve()
        if resolved == project_root or project_root in resolved.parents:
            raise ImportBoundaryError("知识库项目目录不能作为导入来源。")
        return resolved

    def _collect_files(self, target: Path, recursive: bool) -> list[Path]:
        if target.is_file():
            return [target]
        iterator = target.rglob("*") if recursive else target.glob("*")
        files = []
        for path in iterator:
            if not path.is_file() or path.is_symlink():
                continue
            if any(part.casefold() in SKIP_DIRECTORIES for part in path.parts):
                continue
            files.append(path)
            if len(files) > self.settings.max_import_files:
                raise ImportBoundaryError(
                    f"文件数量超过单次上限 {self.settings.max_import_files}，请缩小导入范围。"
                )
        return sorted(files, key=lambda item: str(item).lower())

    def inspect_path(self, raw_path: str, recursive: bool = True) -> dict[str, Any]:
        target = self._validate_target(raw_path)
        files = self._collect_files(target, recursive)
        supported: list[Path] = []
        sensitive: list[Path] = []
        unsupported: list[Path] = []
        total_bytes = 0
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total_bytes += size
            if is_sensitive_path(path):
                sensitive.append(path)
            elif path.suffix.lower() in SUPPORTED_EXTENSIONS:
                supported.append(path)
            else:
                unsupported.append(path)
        return {
            "path": str(target),
            "recursive": recursive,
            "inspection_token": self._inspection_token(target, recursive, files),
            "total_files": len(files),
            "supported_files": len(supported),
            "unsupported_files": len(unsupported),
            "sensitive_files": len(sensitive),
            "total_bytes": total_bytes,
            "supported_sample": [str(path) for path in supported[:20]],
            "unsupported_extensions": sorted(
                {path.suffix.lower() or "(none)" for path in unsupported}
            ),
            "sensitive_names": [path.name for path in sensitive[:20]],
        }

    @staticmethod
    def _inspection_token(target: Path, recursive: bool, files: list[Path]) -> str:
        manifest = []
        for path in files:
            stat = path.stat()
            manifest.append(
                {
                    "path": str(path),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        payload = json.dumps(
            {"target": str(target), "recursive": recursive, "files": manifest},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def import_path(
        self,
        raw_path: str,
        *,
        recursive: bool = True,
        domain: str = "work",
        privacy: str = "private",
        inspection_token: str | None = None,
    ) -> dict[str, Any]:
        target = self._validate_target(raw_path)
        files = self._collect_files(target, recursive)
        if inspection_token:
            current_token = self._inspection_token(target, recursive, files)
            if current_token != inspection_token:
                raise ImportBoundaryError(
                    "资料范围在检查后发生了变化，请重新执行只读检查后再确认导入。"
                )
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []

        for path in files:
            if is_sensitive_path(path):
                skipped.append({"path": str(path), "reason": "sensitive_name"})
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                skipped.append({"path": str(path), "reason": "unsupported_format"})
                continue
            try:
                result = self.import_file(path, domain=domain, privacy=privacy)
                results.append(result)
            except (OSError, ParseError, ValueError) as exc:
                errors.append({"path": str(path), "error": str(exc)})

        imported = sum(1 for item in results if item["status"] == "imported")
        reindexed = sum(1 for item in results if item["status"] == "reindexed")
        duplicates = sum(1 for item in results if item["status"] == "duplicate")
        return {
            "target": str(target),
            "imported": imported,
            "reindexed": reindexed,
            "duplicates": duplicates,
            "skipped": len(skipped),
            "errors": len(errors),
            "items": results,
            "skipped_items": skipped[:100],
            "error_items": errors[:100],
        }

    def import_file(self, path: Path, *, domain: str, privacy: str) -> dict[str, Any]:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
        if stat.st_size > self.settings.max_source_bytes:
            raise ValueError(
                f"文件大小 {stat.st_size} 字节，超过单文件上限 {self.settings.max_source_bytes}。"
            )
        content_hash = sha256_file(resolved)
        existing = self.repository.source_by_hash(content_hash)
        if existing:
            if (
                existing.get("parser_name") != "normalized-text"
                and existing.get("parser_version") != DOCUMENT_PIPELINE_VERSION
            ):
                parsed = parse_file(
                    resolved,
                    settings=self.settings,
                    privacy=str(existing.get("privacy") or privacy),
                )
                chunks = chunk_document(parsed)
                if not chunks:
                    raise ParseError("文件中没有可索引文字。")
                stored = self.repository.replace_document(
                    source_id=existing["id"],
                    parsed=parsed,
                    chunks=chunks,
                )
                return {
                    "status": "reindexed",
                    "path": str(resolved),
                    "content_hash": content_hash,
                    "vault_path": existing["vault_path"],
                    **stored,
                }
            return {
                "status": "duplicate",
                "path": str(resolved),
                "source_id": existing["id"],
                "content_hash": content_hash,
            }

        parsed = parse_file(resolved, settings=self.settings, privacy=privacy)
        chunks = chunk_document(parsed)
        if not chunks:
            raise ParseError("文件中没有可索引文字。")

        suffix = resolved.suffix.lower()
        vault_dir = self.settings.vault_root / content_hash[:2]
        vault_dir.mkdir(parents=True, exist_ok=True)
        vault_path = vault_dir / f"{content_hash}{suffix}"
        if not vault_path.exists():
            shutil.copy2(resolved, vault_path)
            copied_hash = sha256_file(vault_path)
            if copied_hash != content_hash:
                vault_path.unlink(missing_ok=True)
                raise OSError("原始资料复制后的哈希校验失败。")

        created_at = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
        stored = self.repository.add_document(
            original_uri=str(resolved),
            original_name=resolved.name,
            vault_path=str(vault_path),
            source_type=suffix.lstrip(".") or "file",
            content_hash=content_hash,
            byte_size=stat.st_size,
            domain=domain,
            privacy=privacy,
            parsed=parsed,
            chunks=chunks,
            source_created_at=created_at,
        )
        return {
            "status": "imported",
            "path": str(resolved),
            "content_hash": content_hash,
            "vault_path": str(vault_path),
            **stored,
        }

    def reindex_outdated(
        self,
        *,
        limit: int = 10_000,
        source_types: set[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        candidates = self.repository.sources_for_reindex(
            DOCUMENT_PIPELINE_VERSION,
            CHUNKER_VERSION,
            limit=limit,
            source_types=source_types,
            force=force,
        )
        reindexed = 0
        skipped = 0
        errors: dict[str, int] = {}
        for item in candidates:
            path = Path(item["vault_path"])
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                skipped += 1
                continue
            try:
                with self.repository.database.connect() as connection:
                    source = connection.execute(
                        "SELECT content_hash FROM sources WHERE id=?", (item["id"],)
                    ).fetchone()
                if not source or sha256_file(path) != source["content_hash"]:
                    skipped += 1
                    continue
                parsed = parse_file(
                    path,
                    settings=self.settings,
                    privacy=str(item["privacy"]),
                )
                parsed.title = Path(str(item["original_name"])).stem or parsed.title
                if sha256_file(path) != source["content_hash"]:
                    skipped += 1
                    continue
                chunks = chunk_document(parsed)
                if not chunks:
                    raise ParseError("文件中没有可索引文字。")
                self.repository.replace_document(
                    source_id=item["id"],
                    parsed=parsed,
                    chunks=chunks,
                )
                reindexed += 1
            except (OSError, ParseError, ValueError) as exc:
                error_name = type(exc).__name__
                errors[error_name] = errors.get(error_name, 0) + 1
        return {
            "status": "completed" if not errors else "warning",
            "candidates": len(candidates),
            "reindexed": reindexed,
            "skipped": skipped,
            "errors": sum(errors.values()),
            "error_types": errors,
            "parser_version": DOCUMENT_PIPELINE_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "source_types": sorted(source_types or ()),
            "forced": force,
        }

    def import_text(
        self,
        *,
        text: str,
        title: str,
        original_uri: str,
        source_type: str,
        domain: str,
        privacy: str,
        metadata: dict[str, Any] | None = None,
        event_time: str | None = None,
        source_created_at: str | None = None,
    ) -> dict[str, Any]:
        """Store normalized text without materializing an intermediate source file."""
        metadata = metadata or {}
        clean = text.replace("\x00", "").strip()
        if not clean:
            raise ParseError("文本中没有可索引内容。")
        if source_type == "codex-turn":
            codex_metadata = metadata
            if (
                codex_metadata.get("record_kind") != "user_task"
                or codex_metadata.get("assistant_output_indexed") is not False
                or "## Codex 最终回答" in clean
            ):
                raise ImportBoundaryError(
                    "Codex 任务记录只能写入用户请求；助手回答不得进入知识库。"
                )
        existing_uri = self.repository.source_by_uri(original_uri)
        if existing_uri:
            return {
                "status": "duplicate",
                "source_id": existing_uri["id"],
                "content_hash": existing_uri["content_hash"],
                "original_uri": original_uri,
            }

        raw = clean.encode("utf-8")
        if len(raw) > self.settings.max_source_bytes:
            raise ValueError(
                f"文本大小 {len(raw)} 字节，超过单资料上限 {self.settings.max_source_bytes}。"
            )
        content_hash = hashlib.sha256(raw).hexdigest()
        existing_hash = self.repository.source_by_hash(content_hash)
        if existing_hash:
            alias = None
            original_path = str(metadata.get("original_path") or "").strip()
            if original_path:
                alias = self.repository.register_source_alias(
                    source_id=existing_hash["id"],
                    original_uri=original_path,
                    original_name=Path(original_path).name or title.strip() or "未命名资料",
                    vault_path=original_path,
                    source_type=str(metadata.get("original_source_type") or source_type),
                    byte_size=int(
                        metadata.get("original_byte_size") or existing_hash["byte_size"]
                    ),
                    metadata={
                        "original_hash": metadata.get("original_hash"),
                        "processing_level": metadata.get("requested_processing_level", "L1"),
                        "derived_kind": metadata.get("derived_kind"),
                    },
                )
            return {
                "status": "duplicate",
                "source_id": existing_hash["id"],
                "content_hash": content_hash,
                "original_uri": original_uri,
                "alias": alias,
            }

        chunks = chunk_text(clean)
        vault_dir = self.settings.vault_root / content_hash[:2]
        vault_dir.mkdir(parents=True, exist_ok=True)
        vault_path = vault_dir / f"{content_hash}.md"
        if not vault_path.exists():
            vault_path.write_bytes(raw)

        parsed = ParsedDocument(
            title=title.strip() or "未命名文本",
            text=clean,
            parser_name="normalized-text",
            mime_type="text/markdown",
            event_time=event_time,
            metadata=metadata,
        )
        stored = self.repository.add_document(
            original_uri=original_uri,
            original_name=f"{title.strip() or '未命名文本'}.md",
            vault_path=str(vault_path),
            source_type=source_type,
            content_hash=content_hash,
            byte_size=len(raw),
            domain=domain,
            privacy=privacy,
            parsed=parsed,
            chunks=chunks,
            source_created_at=source_created_at or event_time,
        )
        return {
            "status": "imported",
            "content_hash": content_hash,
            "original_uri": original_uri,
            "vault_path": str(vault_path),
            **stored,
        }
