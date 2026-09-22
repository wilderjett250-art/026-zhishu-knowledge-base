import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pkas.config import Settings, get_settings
from pkas.document_policy import document_policy
from pkas.parsers import (
    DOCLING_EXTENSIONS,
    NATIVE_DOCUMENT_EXTENSIONS,
    TEXT_EXTENSIONS,
    VISUAL_EXTENSIONS,
    ParsedBlock,
    ParsedDocument,
    ParseError,
    UnsupportedFormatError,
    parse_native_file,
)

PaddleTransport = Callable[[dict[str, Any]], dict[str, Any]]
LOCAL_OFFICE_CONVERSION_FORMATS = {".doc": "docx", ".xls": "xlsx", ".ppt": "pptx"}
DOCUMENT_PIPELINE_VERSION = "hybrid-v2"


@dataclass(frozen=True, slots=True)
class ExtractionAssessment:
    score: float
    needs_docling: bool
    needs_visual: bool
    visual_pages: tuple[int, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "needs_docling": self.needs_docling,
            "needs_visual": self.needs_visual,
            "visual_pages": list(self.visual_pages),
            "reasons": list(self.reasons),
        }


def assess_extraction(
    path: Path,
    parsed: ParsedDocument | None,
    *,
    min_pdf_page_chars: int = 40,
) -> ExtractionAssessment:
    extension = path.suffix.lower()
    reasons: list[str] = []
    visual_pages: list[int] = []
    needs_docling = extension in DOCLING_EXTENSIONS and parsed is None
    needs_visual = extension in VISUAL_EXTENSIONS
    score = 1.0

    if parsed is None:
        reasons.append("native_parser_unavailable")
        score = 0.0
    else:
        clean_chars = len(re.sub(r"\s+", "", parsed.text))
        if clean_chars == 0:
            reasons.append("empty_native_text")
            score -= 0.75
        if extension in VISUAL_EXTENSIONS:
            reasons.append("visual_pixels_not_read")
            score = min(score, 0.15)
        replacement_count = parsed.text.count("�")
        if replacement_count:
            reasons.append("replacement_characters")
            score -= min(0.3, replacement_count / max(1, clean_chars))
        if parsed.metadata.get("json_structure_not_read"):
            reasons.append("invalid_json_text_fallback")
            score -= 0.15

        if extension == ".pdf":
            for item in parsed.metadata.get("page_stats", []):
                page = int(item.get("page", 0))
                text_chars = int(item.get("text_chars", 0))
                images = int(item.get("images", 0))
                if page > 0 and text_chars < min_pdf_page_chars and (images > 0 or text_chars == 0):
                    visual_pages.append(page)
            if visual_pages:
                reasons.append("low_text_or_scanned_pdf_pages")
                score -= min(0.65, 0.08 * len(visual_pages))
                needs_visual = True

        embedded_images = int(parsed.metadata.get("embedded_image_count", 0))
        image_descriptions = min(
            embedded_images,
            int(parsed.metadata.get("embedded_image_description_count", 0)),
        )
        uncovered_images = embedded_images - image_descriptions
        if uncovered_images and extension in {".docx", ".pptx", ".xlsx"}:
            reasons.append("embedded_images_not_ocrd")
            score -= min(0.2, uncovered_images * 0.02)
            needs_docling = True
        if int(parsed.metadata.get("chart_count", 0)):
            reasons.append("charts_need_semantic_parsing")
            score -= 0.08
            needs_docling = True

    return ExtractionAssessment(
        score=round(max(0.0, score), 4),
        needs_docling=needs_docling,
        needs_visual=needs_visual,
        visual_pages=tuple(visual_pages),
        reasons=tuple(reasons),
    )


def _markdown_blocks(markdown: str, *, prefix: str) -> list[ParsedBlock]:
    blocks: list[ParsedBlock] = []
    buffer: list[str] = []
    buffer_kind = "paragraph"

    def flush() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        if text:
            blocks.append(
                ParsedBlock(
                    kind=buffer_kind,
                    text=text,
                    locator=f"{prefix}:block:{len(blocks) + 1}",
                )
            )
        buffer = []

    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            flush()
            buffer_kind = "paragraph"
            continue
        if line.startswith("#"):
            flush()
            buffer_kind = "heading"
            buffer = [line]
            flush()
            buffer_kind = "paragraph"
            continue
        kind = "table" if line.startswith("|") and line.endswith("|") else "paragraph"
        if buffer and kind != buffer_kind:
            flush()
        buffer_kind = kind
        buffer.append(line)
    flush()
    return blocks


class DoclingExtractor:
    name = "docling"

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("docling") is not None

    def parse(self, path: Path) -> ParsedDocument:
        if not self.available():
            raise ParseError(
                "Docling 解析已启用，但当前环境未安装可选依赖。请安装 pkas[document-ai]。"
            )
        try:
            from docling.document_converter import (  # pyright: ignore[reportMissingImports]
                DocumentConverter,
            )

            result = DocumentConverter().convert(str(path))
            document = result.document
            markdown = str(document.export_to_markdown()).strip()
            if not markdown:
                raise ParseError("Docling 没有返回可索引文字。")
            blocks = _markdown_blocks(markdown, prefix="docling")
            return ParsedDocument(
                title=path.stem,
                text=markdown,
                parser_name=self.name,
                mime_type=None,
                blocks=blocks,
                metadata={"docling_status": str(getattr(result, "status", "completed"))},
            )
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"Docling 解析失败：{exc}") from exc


class LocalOfficeConverter:
    """Convert legacy Office files locally, only while an explicit import reads them."""

    name = "libreoffice-headless"

    @staticmethod
    def executable() -> Path | None:
        candidates = [shutil.which("soffice")]
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(variable)
            if root:
                candidates.append(str(Path(root) / "LibreOffice" / "program" / "soffice.exe"))
        for value in candidates:
            if value and Path(value).is_file():
                return Path(value)
        return None

    def available(self) -> bool:
        return self.executable() is not None

    def parse(self, path: Path, runtime_root: Path) -> ParsedDocument:
        extension = path.suffix.lower()
        target = LOCAL_OFFICE_CONVERSION_FORMATS.get(extension)
        executable = self.executable()
        if target is None:
            raise ParseError("当前旧格式没有本地转换规则。")
        if executable is None:
            raise ParseError("未发现LibreOffice，旧格式无法在本地转换。")
        before = path.stat()
        runtime_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="office-", dir=runtime_root) as temporary:
                output = Path(temporary)
                completed = subprocess.run(
                    [
                        str(executable),
                        "--headless",
                        "--convert-to",
                        target,
                        "--outdir",
                        str(output),
                        str(path),
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=90,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                converted = output / f"{path.stem}.{target}"
                if completed.returncode != 0 or not converted.is_file():
                    raise ParseError("LibreOffice未能转换该旧格式文件。")
                parsed = parse_native_file(converted)
        except subprocess.TimeoutExpired as exc:
            raise ParseError("LibreOffice转换超时，原文件未修改。") from exc
        except ParseError:
            raise
        except OSError as exc:
            raise ParseError("LibreOffice本地转换无法启动。") from exc
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ParseError("转换期间原文件发生变化，请重新预览。")
        parsed.parser_name = f"{self.name}+{parsed.parser_name}"
        parsed.metadata.update(
            converted_from=extension,
            converter=self.name,
            converter_ephemeral=True,
        )
        return parsed


class PaddleOCRExtractor:
    name = "paddleocr-vl-1.6-service"

    def __init__(
        self,
        settings: Settings,
        transport: PaddleTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport or self._http_transport

    def _http_transport(self, payload: dict[str, Any]) -> dict[str, Any]:
        base_url = (self.settings.document_paddleocr_base_url or "").rstrip("/")
        if not base_url:
            raise ParseError("PaddleOCR 服务地址未配置。")
        endpoint = (
            base_url if base_url.endswith("/layout-parsing") else f"{base_url}/layout-parsing"
        )
        headers = {"Content-Type": "application/json"}
        key = self.settings.document_paddleocr_api_key
        if key and key.get_secret_value().strip():
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.settings.document_parser_timeout_seconds,
            ) as response:
                return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            raise ParseError(f"PaddleOCR 服务返回 HTTP {exc.code}。") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ParseError("PaddleOCR 服务连接失败。") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ParseError("PaddleOCR 服务返回了无效 JSON。") from exc

    @staticmethod
    def _blocks_from_response(raw: dict[str, Any], *, page: int) -> list[ParsedBlock]:
        try:
            results = raw["result"]["layoutParsingResults"]
        except (KeyError, TypeError) as exc:
            raise ParseError("PaddleOCR 响应缺少 layoutParsingResults。") from exc
        if not isinstance(results, list):
            raise ParseError("PaddleOCR layoutParsingResults 不是数组。")

        blocks: list[ParsedBlock] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            pruned = result.get("prunedResult") or {}
            parsing_items = (
                pruned.get("parsing_res_list")
                or pruned.get("parsingResList")
                or result.get("parsing_res_list")
                or []
            )
            if isinstance(parsing_items, list):
                for item in parsing_items:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("block_content") or item.get("blockContent") or "").strip()
                    if not text:
                        continue
                    bbox_value = item.get("block_bbox") or item.get("blockBbox")
                    bbox = None
                    if isinstance(bbox_value, (list, tuple)) and len(bbox_value) == 4:
                        bbox = cast(
                            tuple[float, float, float, float],
                            tuple(float(value) for value in bbox_value),
                        )
                    kind = str(item.get("block_label") or item.get("blockLabel") or "paragraph")
                    blocks.append(
                        ParsedBlock(
                            kind=kind,
                            text=text,
                            locator=f"page:{page}/visual-block:{len(blocks) + 1}",
                            page=page,
                            bbox=bbox,
                        )
                    )
            if not blocks:
                markdown = result.get("markdown") or {}
                text = str(markdown.get("text") or "").strip() if isinstance(markdown, dict) else ""
                if text:
                    for block in _markdown_blocks(text, prefix=f"page:{page}/visual"):
                        block.page = page
                        blocks.append(block)
        if not blocks:
            raise ParseError("PaddleOCR 没有返回可索引文字。")
        return blocks

    def _request_image(self, image_bytes: bytes, *, page: int) -> list[ParsedBlock]:
        payload = {
            "file": base64.b64encode(image_bytes).decode("ascii"),
            "fileType": 1,
            "visualize": False,
            "formatBlockContent": True,
        }
        raw = self.transport(payload)
        return self._blocks_from_response(raw, page=page)

    def parse_image(self, path: Path) -> ParsedDocument:
        blocks = self._request_image(path.read_bytes(), page=1)
        return ParsedDocument(
            title=path.stem,
            text="\n\n".join(block.text for block in blocks),
            parser_name=self.name,
            mime_type=None,
            blocks=blocks,
            metadata={"visual_page_indexes": [1]},
        )

    def parse_pdf_pages(self, path: Path, pages: tuple[int, ...]) -> ParsedDocument:
        import fitz

        selected_pages = pages[: self.settings.document_visual_max_pages]
        blocks: list[ParsedBlock] = []
        try:
            document = fitz.open(path)
            zoom = max(1.0, self.settings.document_visual_render_dpi / 72)
            matrix = fitz.Matrix(zoom, zoom)
            for page_number in selected_pages:
                if page_number < 1 or page_number > len(document):
                    continue
                pixmap = document[page_number - 1].get_pixmap(matrix=matrix, alpha=False)
                blocks.extend(self._request_image(pixmap.tobytes("png"), page=page_number))
            document.close()
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"PDF 页面视觉解析失败：{exc}") from exc
        if not blocks:
            raise ParseError("没有可供 PaddleOCR 解析的 PDF 页面。")
        return ParsedDocument(
            title=path.stem,
            text="\n\n".join(block.text for block in blocks),
            parser_name=self.name,
            mime_type="application/pdf",
            blocks=blocks,
            metadata={"visual_page_indexes": list(selected_pages)},
        )


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _merge_documents(
    path: Path,
    native: ParsedDocument | None,
    docling: ParsedDocument | None,
    visual: ParsedDocument | None,
    assessment: ExtractionAssessment,
    warnings: list[str],
    *,
    metadata_limit: int,
) -> ParsedDocument:
    candidates = [item for item in (native, docling, visual) if item is not None]
    if not candidates:
        raise ParseError("没有解析器能够从该文件提取可索引内容。")
    base = native or docling or visual
    assert base is not None
    blocks = list(base.blocks)

    if docling is not None and docling is not base:
        existing = {_normalized(block.text) for block in blocks if block.text.strip()}
        for block in docling.blocks:
            key = _normalized(block.text)
            if key and key not in existing:
                blocks.append(block)
                existing.add(key)

    if visual is not None and visual is not base:
        visual_pages = {block.page for block in visual.blocks if block.page is not None}
        if visual_pages:
            blocks = [block for block in blocks if block.page not in visual_pages]
        existing = {(block.page, _normalized(block.text)) for block in blocks if block.text.strip()}
        for block in visual.blocks:
            key = (block.page, _normalized(block.text))
            if key[1] and key not in existing:
                blocks.append(block)
                existing.add(key)

    indexed_blocks = list(enumerate(blocks))
    if any(block.page is not None for block in blocks):
        indexed_blocks.sort(
            key=lambda item: (
                item[1].page if item[1].page is not None else 1_000_000,
                item[0],
            )
        )
        blocks = [block for _index, block in indexed_blocks]

    text_parts: list[str] = []
    previous_code = False
    for block in blocks:
        block_text = block.text.rstrip() if block.kind == "code" else block.text.strip()
        if not block_text.strip():
            continue
        if text_parts:
            text_parts.append("\n" if previous_code and block.kind == "code" else "\n\n")
        text_parts.append(block_text)
        previous_code = block.kind == "code"
    text = "".join(text_parts).strip()
    if not text:
        raise ParseError("文件中没有可索引文字。")
    extractors = [candidate.parser_name for candidate in candidates]
    metadata = dict(base.metadata)
    metadata["source_extension"] = path.suffix.lower()
    metadata["extraction"] = {
        "pipeline_version": DOCUMENT_PIPELINE_VERSION,
        "extractors": extractors,
        "initial_quality": assessment.as_dict(),
        "warnings": warnings,
        "block_count": len(blocks),
        "block_index_truncated": len(blocks) > metadata_limit,
    }
    metadata["block_index"] = [block.as_metadata() for block in blocks[: max(0, metadata_limit)]]
    return ParsedDocument(
        title=base.title or path.stem,
        text=text,
        parser_name="+".join(extractors),
        parser_version=DOCUMENT_PIPELINE_VERSION,
        mime_type=base.mime_type,
        language=base.language,
        event_time=base.event_time,
        metadata=metadata,
        messages=base.messages,
        blocks=blocks,
    )


class DocumentExtractionPipeline:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        paddle_transport: PaddleTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.docling = DoclingExtractor()
        self.office = LocalOfficeConverter()
        self.paddle = PaddleOCRExtractor(self.settings, paddle_transport)

    def parse(self, path: Path, *, privacy: str = "private") -> ParsedDocument:
        policy = document_policy(self.settings)
        extension = path.suffix.lower()
        native: ParsedDocument | None = None
        docling: ParsedDocument | None = None
        visual: ParsedDocument | None = None
        warnings: list[str] = []

        if (
            extension in TEXT_EXTENSIONS
            or extension in NATIVE_DOCUMENT_EXTENSIONS
            or extension in VISUAL_EXTENSIONS
        ):
            try:
                native = parse_native_file(path)
            except UnsupportedFormatError:
                native = None
        elif extension in LOCAL_OFFICE_CONVERSION_FORMATS:
            try:
                runtime_root = self.settings.data_root / "runtime" / "office-conversion"
                native = self.office.parse(path, runtime_root)
            except ParseError as exc:
                warnings.append(f"libreoffice:{type(exc).__name__}")

        assessment = assess_extraction(
            path,
            native,
            min_pdf_page_chars=self.settings.document_visual_min_page_chars,
        )

        if policy["docling_enabled"] and (assessment.needs_docling or native is None):
            try:
                docling = self.docling.parse(path)
            except ParseError as exc:
                warnings.append(f"docling:{type(exc).__name__}")
        elif assessment.needs_docling:
            warnings.append("docling:not_enabled")

        remote_allowed = policy["paddleocr_enabled"] and (
            privacy != "restricted" or self.settings.document_allow_restricted_remote_processing
        )
        if assessment.needs_visual and remote_allowed:
            try:
                if extension in VISUAL_EXTENSIONS:
                    visual = self.paddle.parse_image(path)
                elif extension == ".pdf" and assessment.visual_pages:
                    visual = self.paddle.parse_pdf_pages(path, assessment.visual_pages)
            except ParseError as exc:
                warnings.append(f"paddleocr:{type(exc).__name__}")
        elif assessment.needs_visual:
            warnings.append("paddleocr:remote_processing_not_enabled")

        result = _merge_documents(
            path,
            native,
            docling,
            visual,
            assessment,
            warnings,
            metadata_limit=self.settings.document_block_metadata_limit,
        )
        result.metadata["document_policy"] = policy
        return result
