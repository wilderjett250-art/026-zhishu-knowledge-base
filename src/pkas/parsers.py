import csv
import json
import mimetypes
from dataclasses import dataclass, field
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
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {
    ".csv",
    ".docx",
    ".htm",
    ".html",
    ".json",
    ".jsonl",
    ".pdf",
    ".xlsx",
}


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
class ParsedDocument:
    title: str
    text: str
    parser_name: str
    parser_version: str = "1"
    mime_type: str | None = None
    language: str | None = None
    event_time: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    messages: list[ParsedMessage] = field(default_factory=list)


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


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


def _messages_to_text(messages: list[ParsedMessage]) -> str:
    lines: list[str] = []
    for message in messages:
        prefix_parts = [value for value in (message.sent_at, message.speaker) if value]
        prefix = " · ".join(prefix_parts)
        lines.append(f"[{prefix}] {message.text}" if prefix else message.text)
    return "\n".join(lines)


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
    return ParsedDocument(
        title=path.stem,
        text=text,
        parser_name="jsonl" if json_lines else "json",
        mime_type="application/x-ndjson" if json_lines else "application/json",
        messages=messages,
        metadata={"record_count": len(candidates)},
    )


def _parse_csv(path: Path) -> ParsedDocument:
    text = _decode_text(path.read_bytes())
    rows = list(csv.reader(text.splitlines()))
    rendered = "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
    return ParsedDocument(
        title=path.stem,
        text=rendered,
        parser_name="csv",
        mime_type="text/csv",
        metadata={"row_count": len(rows)},
    )


def _parse_html(path: Path) -> ParsedDocument:
    from bs4 import BeautifulSoup

    html = _decode_text(path.read_bytes())
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else path.stem
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    return ParsedDocument(
        title=title,
        text=text,
        parser_name="beautifulsoup",
        mime_type="text/html",
    )


def _parse_pdf(path: Path) -> ParsedDocument:
    import fitz

    try:
        document = fitz.open(path)
        pages = [cast(str, page.get_text("text")) for page in document]
        metadata = document.metadata or {}
        title = metadata.get("title") or path.stem
        return ParsedDocument(
            title=title,
            text="\n\n".join(pages),
            parser_name="pymupdf",
            mime_type="application/pdf",
            metadata={"page_count": len(document), "pdf_metadata": metadata},
        )
    except Exception as exc:
        raise ParseError(f"PDF 解析失败：{exc}") from exc


def _parse_docx(path: Path) -> ParsedDocument:
    from docx import Document

    try:
        document = Document(str(path))
        lines = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
        for table in document.tables:
            for row in table.rows:
                lines.append(" | ".join(cell.text.strip() for cell in row.cells))
        return ParsedDocument(
            title=path.stem,
            text="\n".join(lines),
            parser_name="python-docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except Exception as exc:
        raise ParseError(f"DOCX 解析失败：{exc}") from exc


def _parse_xlsx(path: Path) -> ParsedDocument:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
        lines: list[str] = []
        sheet_rows: dict[str, int] = {}
        for sheet in workbook.worksheets:
            lines.append(f"## {sheet.title}")
            count = 0
            for row in sheet.iter_rows(values_only=True):
                values = ["" if value is None else str(value) for value in row]
                if any(value.strip() for value in values):
                    lines.append(" | ".join(values))
                    count += 1
            sheet_rows[sheet.title] = count
        workbook.close()
        return ParsedDocument(
            title=path.stem,
            text="\n".join(lines),
            parser_name="openpyxl",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            metadata={"sheet_rows": sheet_rows},
        )
    except Exception as exc:
        raise ParseError(f"XLSX 解析失败：{exc}") from exc


def parse_file(path: Path) -> ParsedDocument:
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(f"暂不支持该格式：{extension or '无扩展名'}")

    if extension in TEXT_EXTENSIONS:
        text = _decode_text(path.read_bytes())
        mime_type = mimetypes.guess_type(path.name)[0] or "text/plain"
        return ParsedDocument(
            title=path.stem,
            text=text,
            parser_name="plain-text",
            mime_type=mime_type,
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
    raise UnsupportedFormatError(f"暂不支持该格式：{extension}")
