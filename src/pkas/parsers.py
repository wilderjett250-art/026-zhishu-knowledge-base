import csv
import json
import mimetypes
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from itertools import zip_longest
from pathlib import Path
from typing import Any, cast

TEXT_EXTENSIONS = {
    ".bat",
    ".c",
    ".cfg",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".ini",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".log",
    ".md",
    ".ps1",
    ".py",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".svelte",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
NATIVE_DOCUMENT_EXTENSIONS = {
    ".csv",
    ".docx",
    ".eml",
    ".htm",
    ".html",
    ".json",
    ".jsonl",
    ".pdf",
    ".pptx",
    ".xlsx",
}
VISUAL_EXTENSIONS = {
    ".bmp",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
DOCLING_EXTENSIONS = {
    ".doc",
    ".epub",
    ".latex",
    ".msg",
    ".odp",
    ".ods",
    ".odt",
    ".ppt",
    ".tex",
    ".xls",
}
SUPPORTED_EXTENSIONS = (
    TEXT_EXTENSIONS | NATIVE_DOCUMENT_EXTENSIONS | VISUAL_EXTENSIONS | DOCLING_EXTENSIONS
)


class UnsupportedFormatError(ValueError):
    pass


class ParseError(ValueError):
    pass


@dataclass(slots=True)
class ParsedMessage:
    sequence: int
    text: str
    speaker: str | None = None
    sent_at: str | None = None
    conversation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedBlock:
    kind: str
    text: str
    locator: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_metadata(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "kind": self.kind,
            "locator": self.locator,
            "char_count": len(self.text),
        }
        if self.page is not None:
            item["page"] = self.page
        if self.bbox is not None:
            item["bbox"] = [round(value, 2) for value in self.bbox]
        if self.metadata:
            item["metadata"] = self.metadata
        return item


@dataclass(slots=True)
class ParsedDocument:
    title: str
    text: str
    parser_name: str
    parser_version: str = "2"
    mime_type: str | None = None
    language: str | None = None
    event_time: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    messages: list[ParsedMessage] = field(default_factory=list)
    blocks: list[ParsedBlock] = field(default_factory=list)


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clean_text(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value.replace("\x00", " ")).strip()


def _line_blocks(text: str, *, prefix: str = "paragraph") -> list[ParsedBlock]:
    blocks: list[ParsedBlock] = []
    for index, raw in enumerate(text.splitlines(), start=1):
        clean = _clean_text(raw)
        if not clean:
            continue
        kind = "heading" if clean.startswith("#") else "paragraph"
        blocks.append(ParsedBlock(kind=kind, text=clean, locator=f"{prefix}:{index}"))
    return blocks


def _document_text(blocks: list[ParsedBlock]) -> str:
    return "\n\n".join(block.text for block in blocks if block.text.strip()).strip()


def _suppress_repeated_pdf_margins(
    blocks: list[ParsedBlock], page_count: int
) -> tuple[list[ParsedBlock], int]:
    if page_count < 3:
        return blocks, 0
    candidates = Counter(
        re.sub(r"\s+", "", block.text).lower()
        for block in blocks
        if block.metadata.get("page_zone") in {"header", "footer"}
        and 1 <= len(re.sub(r"\s+", "", block.text)) <= 120
    )
    threshold = max(3, (page_count * 3 + 4) // 5)
    repeated = {text for text, count in candidates.items() if count >= threshold}
    kept = [
        block
        for block in blocks
        if re.sub(r"\s+", "", block.text).lower() not in repeated
        or block.metadata.get("page_zone") not in {"header", "footer"}
    ]
    return kept, len(blocks) - len(kept)


def _message_from_mapping(item: dict[str, Any], sequence: int) -> ParsedMessage | None:
    text_value = item.get("text") or item.get("content") or item.get("message") or item.get("body")
    if text_value is None:
        return None
    if not isinstance(text_value, str):
        text_value = json.dumps(text_value, ensure_ascii=False)
    speaker = item.get("speaker") or item.get("sender") or item.get("from") or item.get("role")
    sent_at = item.get("timestamp") or item.get("sent_at") or item.get("time") or item.get("date")
    conversation_id = (
        item.get("conversation_id")
        or item.get("chat_id")
        or item.get("thread_id")
        or item.get("room")
    )
    excluded = {
        "text",
        "content",
        "message",
        "body",
        "speaker",
        "sender",
        "from",
        "role",
        "timestamp",
        "sent_at",
        "time",
        "date",
        "conversation_id",
        "chat_id",
        "thread_id",
        "room",
    }
    metadata = {key: value for key, value in item.items() if key not in excluded}
    return ParsedMessage(
        sequence=sequence,
        text=text_value.strip(),
        speaker=str(speaker) if speaker is not None else None,
        sent_at=str(sent_at) if sent_at is not None else None,
        conversation_id=str(conversation_id) if conversation_id is not None else None,
        metadata=metadata,
    )


def _message_to_text(message: ParsedMessage) -> str:
    prefix_parts = [value for value in (message.sent_at, message.speaker) if value]
    prefix = " · ".join(prefix_parts)
    return f"[{prefix}] {message.text}" if prefix else message.text


def _messages_to_text(messages: list[ParsedMessage]) -> str:
    return "\n".join(_message_to_text(message) for message in messages)


def _parse_json(path: Path, json_lines: bool = False) -> ParsedDocument:
    raw_text = _decode_text(path.read_bytes())
    try:
        if json_lines:
            data: Any = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
        else:
            data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"JSON 解析失败：{exc}") from exc

    candidates: list[Any]
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        nested = data.get("messages") or data.get("items") or data.get("records")
        candidates = nested if isinstance(nested, list) else [data]
    else:
        candidates = []

    messages = [
        message
        for index, item in enumerate(candidates)
        if isinstance(item, dict)
        if (message := _message_from_mapping(item, index)) is not None
    ]
    text = (
        _messages_to_text(messages) if messages else json.dumps(data, ensure_ascii=False, indent=2)
    )
    blocks = [
        ParsedBlock(
            kind="message",
            text=_message_to_text(message),
            locator=f"record:{message.sequence}",
        )
        for message in messages
    ] or _line_blocks(text, prefix="json-line")
    return ParsedDocument(
        title=path.stem,
        text=text,
        parser_name="jsonl" if json_lines else "json",
        mime_type="application/x-ndjson" if json_lines else "application/json",
        messages=messages,
        blocks=blocks,
        metadata={"record_count": len(candidates)},
    )


def _parse_csv(path: Path) -> ParsedDocument:
    text = _decode_text(path.read_bytes())
    rows = list(csv.reader(text.splitlines()))
    blocks = [
        ParsedBlock(
            kind="table-row",
            text=" | ".join(cell.strip() for cell in row),
            locator=f"row:{index}",
        )
        for index, row in enumerate(rows, start=1)
        if any(cell.strip() for cell in row)
    ]
    return ParsedDocument(
        title=path.stem,
        text=_document_text(blocks),
        parser_name="csv",
        mime_type="text/csv",
        blocks=blocks,
        metadata={"row_count": len(rows)},
    )


def _parse_html(path: Path) -> ParsedDocument:
    from bs4 import BeautifulSoup

    html = _decode_text(path.read_bytes())
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else path.stem
    blocks: list[ParsedBlock] = []
    index = 0
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr"]):
        if tag.name in {"p", "li"} and tag.find_parent("tr"):
            continue
        clean = _clean_text(tag.get_text(" ", strip=True))
        if not clean:
            continue
        index += 1
        if tag.name and tag.name.startswith("h"):
            kind = "heading"
        elif tag.name == "tr":
            kind = "table-row"
        else:
            kind = "paragraph"
        blocks.append(ParsedBlock(kind=kind, text=clean, locator=f"html:{index}"))
    if not blocks:
        text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
        blocks = _line_blocks(text, prefix="html-line")
    return ParsedDocument(
        title=title,
        text=_document_text(blocks),
        parser_name="beautifulsoup",
        mime_type="text/html",
        blocks=blocks,
    )


def _parse_pdf(path: Path) -> ParsedDocument:
    import fitz

    try:
        document = fitz.open(path)
        metadata = dict(document.metadata or {})
        blocks: list[ParsedBlock] = []
        page_stats: list[dict[str, Any]] = []
        visual_pages: list[int] = []
        for zero_based_page in range(len(document)):
            page_index = zero_based_page + 1
            page = document[zero_based_page]
            page_height = float(page.rect.height)
            page_blocks = cast(list[tuple[Any, ...]], page.get_text("blocks", sort=True))
            page_text_chars = 0
            text_block_count = 0
            for block_index, raw in enumerate(page_blocks, start=1):
                if len(raw) < 7 or int(raw[6]) != 0:
                    continue
                clean = _clean_text(str(raw[4]))
                if not clean:
                    continue
                bbox = tuple(float(value) for value in raw[:4])
                page_zone = (
                    "header"
                    if bbox[1] <= page_height * 0.12
                    else "footer"
                    if bbox[3] >= page_height * 0.88
                    else "body"
                )
                blocks.append(
                    ParsedBlock(
                        kind="paragraph",
                        text=clean,
                        locator=f"page:{page_index}/block:{block_index}",
                        page=page_index,
                        bbox=cast(tuple[float, float, float, float], bbox),
                        metadata={"page_zone": page_zone},
                    )
                )
                page_text_chars += len(clean)
                text_block_count += 1
            image_count = len(page.get_images(full=True))
            if page_text_chars < 40 and (image_count > 0 or page_text_chars == 0):
                visual_pages.append(page_index)
            page_stats.append(
                {
                    "page": page_index,
                    "text_chars": page_text_chars,
                    "text_blocks": text_block_count,
                    "images": image_count,
                }
            )
        blocks, suppressed_margin_blocks = _suppress_repeated_pdf_margins(
            blocks, len(document)
        )
        title = metadata.get("title") or path.stem
        result = ParsedDocument(
            title=title,
            text=_document_text(blocks),
            parser_name="pymupdf-blocks",
            mime_type="application/pdf",
            blocks=blocks,
            metadata={
                "page_count": len(document),
                "page_stats": page_stats,
                "visual_page_indexes": visual_pages,
                "suppressed_repeated_margin_blocks": suppressed_margin_blocks,
                "pdf_metadata": metadata,
            },
        )
        document.close()
        return result
    except Exception as exc:
        raise ParseError(f"PDF 解析失败：{exc}") from exc


def _supplemental_docx_blocks(path: Path, existing_text: str) -> list[ParsedBlock]:
    from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

    existing = re.sub(r"\s+", "", existing_text)
    supplemental: list[ParsedBlock] = []
    part_index = 0
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        parts = sorted(
            name
            for name in names
            if re.fullmatch(
                r"word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml",
                name,
            )
        )
        if "word/document.xml" in names:
            root = etree.fromstring(archive.read("word/document.xml"))
            for text_box in root.xpath(".//*[local-name()='txbxContent']"):
                part_index += 1
                text = " ".join(
                    value.strip()
                    for value in text_box.xpath(".//*[local-name()='t']/text()")
                    if value.strip()
                )
                normalized = re.sub(r"\s+", "", text)
                if normalized and normalized not in existing:
                    supplemental.append(
                        ParsedBlock(
                            kind="text-box",
                            text=text,
                            locator=f"docx:text-box:{part_index}",
                        )
                    )
                    existing += normalized
        for part in parts:
            root = etree.fromstring(archive.read(part))
            part_kind = Path(part).stem.rstrip("0123456789")
            for paragraph_index, paragraph in enumerate(
                root.xpath(".//*[local-name()='p']"),
                start=1,
            ):
                text = "".join(paragraph.xpath(".//*[local-name()='t']/text()"))
                clean = _clean_text(text)
                normalized = re.sub(r"\s+", "", clean)
                if clean and normalized not in existing:
                    supplemental.append(
                        ParsedBlock(
                            kind=part_kind,
                            text=clean,
                            locator=f"docx:{Path(part).name}/paragraph:{paragraph_index}",
                        )
                    )
                    existing += normalized
    return supplemental


def _parse_docx(path: Path) -> ParsedDocument:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = Document(str(path))
        blocks: list[ParsedBlock] = []
        paragraph_index = 0
        table_index = 0
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                paragraph = Paragraph(child, document)
                clean = _clean_text(paragraph.text)
                if not clean:
                    continue
                paragraph_index += 1
                style_name = str(paragraph.style.name or "") if paragraph.style else ""
                kind = "heading" if style_name.lower().startswith("heading") else "paragraph"
                blocks.append(
                    ParsedBlock(
                        kind=kind,
                        text=clean,
                        locator=f"docx:paragraph:{paragraph_index}",
                        metadata={"style": style_name} if style_name else {},
                    )
                )
            elif child.tag.endswith("}tbl"):
                table_index += 1
                table = Table(child, document)
                for row_index, row in enumerate(table.rows, start=1):
                    cells: list[str] = []
                    for cell in row.cells:
                        value = _clean_text(cell.text)
                        if not cells or value != cells[-1]:
                            cells.append(value)
                    text = " | ".join(cells)
                    if text.strip(" |"):
                        blocks.append(
                            ParsedBlock(
                                kind="table-row",
                                text=text,
                                locator=f"docx:table:{table_index}/row:{row_index}",
                                metadata={
                                    "table_id": f"docx:table:{table_index}",
                                    "row_index": row_index,
                                    "is_header": row_index == 1,
                                },
                            )
                        )
        blocks.extend(_supplemental_docx_blocks(path, _document_text(blocks)))
        with zipfile.ZipFile(path) as archive:
            media_count = sum(1 for name in archive.namelist() if name.startswith("word/media/"))
        return ParsedDocument(
            title=path.stem,
            text=_document_text(blocks),
            parser_name="python-docx-structured",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            blocks=blocks,
            metadata={
                "paragraph_count": paragraph_index,
                "table_count": table_index,
                "embedded_image_count": media_count,
            },
        )
    except Exception as exc:
        raise ParseError(f"DOCX 解析失败：{exc}") from exc


def _xlsx_comments(path: Path) -> list[str]:
    from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

    comments: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not re.fullmatch(r"xl/comments\d*\.xml", name):
                continue
            root = etree.fromstring(archive.read(name))
            for comment in root.xpath(".//*[local-name()='comment']"):
                text = "".join(comment.xpath(".//*[local-name()='t']/text()"))
                clean = _clean_text(text)
                if clean:
                    reference = comment.get("ref") or "unknown"
                    comments.append(f"{reference}: {clean}")
    return comments


def _parse_xlsx(path: Path) -> ParsedDocument:
    from openpyxl import load_workbook

    try:
        values_book = load_workbook(path, read_only=True, data_only=True)
        formulas_book = load_workbook(path, read_only=True, data_only=False)
        blocks: list[ParsedBlock] = []
        sheet_rows: dict[str, int] = {}
        formula_count = 0
        for value_sheet in values_book.worksheets:
            formula_sheet = formulas_book[value_sheet.title]
            blocks.append(
                ParsedBlock(
                    kind="heading",
                    text=f"工作表：{value_sheet.title}",
                    locator=f"sheet:{value_sheet.title}",
                )
            )
            count = 0
            header_seen = False
            value_rows = value_sheet.iter_rows()
            formula_rows = formula_sheet.iter_rows()
            for row_index, pair in enumerate(
                zip_longest(value_rows, formula_rows, fillvalue=()),
                start=1,
            ):
                value_row, formula_row = pair
                rendered: list[str] = []
                for value_cell, formula_cell in zip_longest(
                    value_row,
                    formula_row,
                    fillvalue=None,
                ):
                    cell = formula_cell or value_cell
                    if cell is None:
                        continue
                    value = value_cell.value if value_cell is not None else None
                    formula = formula_cell.value if formula_cell is not None else None
                    if value is None and formula is None:
                        continue
                    display = "" if value is None else str(value)
                    if isinstance(formula, str) and formula.startswith("="):
                        formula_count += 1
                        display = f"{display} [公式 {formula}]".strip()
                    rendered.append(f"{cell.coordinate}={display}")
                if rendered:
                    is_header = not header_seen
                    header_seen = True
                    blocks.append(
                        ParsedBlock(
                            kind="table-row",
                            text=" | ".join(rendered),
                            locator=f"sheet:{value_sheet.title}/row:{row_index}",
                            metadata={
                                "table_id": f"sheet:{value_sheet.title}",
                                "row_index": row_index,
                                "is_header": is_header,
                            },
                        )
                    )
                    count += 1
            sheet_rows[value_sheet.title] = count
        for index, comment in enumerate(_xlsx_comments(path), start=1):
            blocks.append(
                ParsedBlock(kind="comment", text=comment, locator=f"xlsx:comment:{index}")
            )
        values_book.close()
        formulas_book.close()
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            image_count = sum(1 for name in names if name.startswith("xl/media/"))
            chart_count = sum(1 for name in names if name.startswith("xl/charts/chart"))
        return ParsedDocument(
            title=path.stem,
            text=_document_text(blocks),
            parser_name="openpyxl-structured",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            blocks=blocks,
            metadata={
                "sheet_rows": sheet_rows,
                "formula_count": formula_count,
                "embedded_image_count": image_count,
                "chart_count": chart_count,
            },
        )
    except Exception as exc:
        raise ParseError(f"XLSX 解析失败：{exc}") from exc


def _office_xml_paragraphs(raw: bytes) -> list[tuple[str, bool]]:
    from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

    root = etree.fromstring(raw)
    paragraphs: list[tuple[str, bool]] = []
    for paragraph in root.xpath(".//*[local-name()='p']"):
        values = paragraph.xpath(".//*[local-name()='t']/text()")
        clean = _clean_text("".join(values))
        if not clean:
            continue
        in_table = any(ancestor.tag.endswith("}tc") for ancestor in paragraph.iterancestors())
        paragraphs.append((clean, in_table))
    return paragraphs


def _parse_pptx(path: Path) -> ParsedDocument:
    try:
        blocks: list[ParsedBlock] = []
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()

            def numbered_part_index(name: str) -> int:
                match = re.search(r"(\d+)", Path(name).stem)
                return int(match.group(1)) if match else 0

            slide_names = sorted(
                (name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                key=numbered_part_index,
            )
            for slide_index, name in enumerate(slide_names, start=1):
                paragraphs = _office_xml_paragraphs(archive.read(name))
                for paragraph_index, (text, in_table) in enumerate(paragraphs, start=1):
                    kind = (
                        "table-row"
                        if in_table
                        else ("heading" if paragraph_index == 1 else "paragraph")
                    )
                    blocks.append(
                        ParsedBlock(
                            kind=kind,
                            text=text,
                            locator=f"slide:{slide_index}/block:{paragraph_index}",
                            page=slide_index,
                        )
                    )
            note_names = sorted(
                name for name in names if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name)
            )
            for note_index, name in enumerate(note_names, start=1):
                for paragraph_index, (text, _in_table) in enumerate(
                    _office_xml_paragraphs(archive.read(name)),
                    start=1,
                ):
                    blocks.append(
                        ParsedBlock(
                            kind="speaker-note",
                            text=text,
                            locator=f"slide-note:{note_index}/paragraph:{paragraph_index}",
                            page=note_index,
                        )
                    )
            image_count = sum(1 for name in names if name.startswith("ppt/media/"))
        return ParsedDocument(
            title=path.stem,
            text=_document_text(blocks),
            parser_name="openxml-pptx-structured",
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            blocks=blocks,
            metadata={
                "slide_count": len(slide_names),
                "speaker_note_count": len(note_names),
                "embedded_image_count": image_count,
            },
        )
    except Exception as exc:
        raise ParseError(f"PPTX 解析失败：{exc}") from exc


def _parse_eml(path: Path) -> ParsedDocument:
    try:
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        blocks: list[ParsedBlock] = []
        subject = str(message.get("subject") or path.stem)
        for header in ("from", "to", "cc", "date", "subject"):
            value = message.get(header)
            if value:
                blocks.append(
                    ParsedBlock(
                        kind="email-header",
                        text=f"{header.title()}: {value}",
                        locator=f"email:header:{header}",
                    )
                )
        attachments: list[str] = []
        body_index = 0
        for part in message.walk() if message.is_multipart() else [message]:
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                attachments.append(part.get_filename() or "unnamed")
                continue
            if part.get_content_type() not in {"text/plain", "text/html"}:
                continue
            content = part.get_content()
            if not isinstance(content, str):
                continue
            if part.get_content_type() == "text/html":
                from bs4 import BeautifulSoup

                content = BeautifulSoup(content, "lxml").get_text("\n")
            for block in _line_blocks(content, prefix=f"email:body:{body_index}"):
                blocks.append(block)
            body_index += 1
        return ParsedDocument(
            title=subject,
            text=_document_text(blocks),
            parser_name="email-stdlib",
            mime_type="message/rfc822",
            event_time=str(message.get("date")) if message.get("date") else None,
            blocks=blocks,
            metadata={
                "attachment_count": len(attachments),
                "attachment_names": attachments,
            },
        )
    except Exception as exc:
        raise ParseError(f"EML 解析失败：{exc}") from exc


def parse_native_file(path: Path) -> ParsedDocument:
    extension = path.suffix.lower()
    if extension in TEXT_EXTENSIONS:
        text = _decode_text(path.read_bytes())
        mime_type = mimetypes.guess_type(path.name)[0] or "text/plain"
        blocks = _line_blocks(text)
        return ParsedDocument(
            title=path.stem,
            text=text,
            parser_name="plain-text",
            mime_type=mime_type,
            blocks=blocks,
        )
    if extension == ".csv":
        return _parse_csv(path)
    if extension == ".json":
        return _parse_json(path)
    if extension == ".jsonl":
        return _parse_json(path, json_lines=True)
    if extension in {".html", ".htm"}:
        return _parse_html(path)
    if extension == ".pdf":
        return _parse_pdf(path)
    if extension == ".docx":
        return _parse_docx(path)
    if extension == ".xlsx":
        return _parse_xlsx(path)
    if extension == ".pptx":
        return _parse_pptx(path)
    if extension == ".eml":
        return _parse_eml(path)
    raise UnsupportedFormatError(f"原生解析器不支持该格式：{extension or '无扩展名'}")


def parse_file(
    path: Path,
    *,
    settings: Any | None = None,
    privacy: str = "private",
    paddle_transport: Any | None = None,
) -> ParsedDocument:
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(f"暂不支持该格式：{path.suffix.lower() or '无扩展名'}")
    from pkas.document_extraction import DocumentExtractionPipeline

    return DocumentExtractionPipeline(
        settings=settings,
        paddle_transport=paddle_transport,
    ).parse(path, privacy=privacy)
