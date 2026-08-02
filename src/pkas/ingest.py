import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pkas.config import Settings, get_settings
from pkas.parsers import SUPPORTED_EXTENSIONS, ParseError, parse_file
from pkas.repository import Repository

SKIP_DIRECTORIES = {
    ".git",
    ".idea",
    ".venv",
    ".vscode",
    "__pycache__",
    "node_modules",
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


def chunk_text(text: str, max_chars: int = 1800, overlap: int = 180) -> list[dict[str, Any]]:
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
            if any(part in SKIP_DIRECTORIES for part in path.parts):
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
        duplicates = sum(1 for item in results if item["status"] == "duplicate")
        return {
            "target": str(target),
            "imported": imported,
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
            return {
                "status": "duplicate",
                "path": str(resolved),
                "source_id": existing["id"],
                "content_hash": content_hash,
            }

        parsed = parse_file(resolved)
        chunks = chunk_text(parsed.text)
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
