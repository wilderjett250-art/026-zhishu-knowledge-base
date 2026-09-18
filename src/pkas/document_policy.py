"""Document enhancement consent, independent of embedding and agent settings."""

import json
import os
import shutil
from importlib.util import find_spec
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel, StrictBool

from pkas.config import Settings
from pkas.parsers import (
    DOCLING_EXTENSIONS,
    NATIVE_DOCUMENT_EXTENSIONS,
    OOXML_PRESENTATION_EXTENSIONS,
    OOXML_SHEET_EXTENSIONS,
    OOXML_WORD_EXTENSIONS,
    VISUAL_EXTENSIONS,
)

LOCAL_OFFICE_CONVERTER_FORMATS = {".doc", ".ppt", ".xls"}


class DocumentPolicyRequest(BaseModel):
    ai_enhancement_enabled: StrictBool


def format_capabilities() -> dict:
    """Expose format boundaries from the real parser registry, not UI copy.

    This is intentionally capability-only: it does not enumerate local files,
    inspect a document, start a converter or make a network request.
    """
    native_groups = (
        ("办公文档", OOXML_WORD_EXTENSIONS | {".odt", ".rtf", ".pdf"}),
        ("表格", OOXML_SHEET_EXTENSIONS | {".csv", ".tsv", ".ods"}),
        ("演示文稿", OOXML_PRESENTATION_EXTENSIONS | {".odp"}),
        ("邮件与网页数据", {".eml", ".htm", ".html", ".json", ".jsonl"}),
    )
    return {
        "native_groups": [
            {
                "label": label,
                "extensions": sorted(group & NATIVE_DOCUMENT_EXTENSIONS),
            }
            for label, group in native_groups
        ],
        "visual_review_formats": sorted(VISUAL_EXTENSIONS | {".pdf"}),
        "local_converter_formats": sorted(LOCAL_OFFICE_CONVERTER_FORMATS),
        "local_converter_available": shutil.which("soffice") is not None,
        "optional_converter_formats": sorted(DOCLING_EXTENSIONS - LOCAL_OFFICE_CONVERTER_FORMATS),
        "optional_converter_available": find_spec("docling") is not None,
    }


def policy_path(settings: Settings) -> Path:
    return settings.data_root / "config" / "document_parsing_policy.json"


def document_policy(settings: Settings) -> dict:
    enabled = settings.document_ai_enhancement_enabled
    warning = None
    path = policy_path(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        enabled = DocumentPolicyRequest.model_validate(payload).ai_enhancement_enabled
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        enabled = False
        warning = "解析设置无法读取，已安全回退到本地解析。"
    return {
        "ai_enhancement_enabled": enabled,
        "mode": "configured_enhancement" if enabled else "local",
        "luna_available": False,
        "docling_enabled": enabled and settings.document_docling_enabled,
        "paddleocr_enabled": enabled and settings.paddleocr_enabled,
        "warning": warning,
        "scope": "document_extraction_only",
        "format_capabilities": format_capabilities(),
    }


def disable_enhancement(settings: Settings) -> dict:
    path = policy_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump({"ai_enhancement_enabled": False}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return document_policy(settings)
