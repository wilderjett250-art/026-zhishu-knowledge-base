"""Document enhancement consent, independent of embedding and agent settings."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel, StrictBool

from pkas.config import Settings


class DocumentPolicyRequest(BaseModel):
    ai_enhancement_enabled: StrictBool


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
