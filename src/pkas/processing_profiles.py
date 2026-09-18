"""User-facing intake profiles for deciding how deeply to process local files.

The profiles are deliberately policy-only.  They do not scan files or call an
LLM; the intake service applies the selected rules after the user confirms the
preview.  Keeping this layer deterministic prevents a model from silently
changing the scope of a local import.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from pydantic import BaseModel, Field

from pkas.config import Settings

PROFILE_IDS = ("work_efficiency", "complete_personal", "lightweight", "custom")
RULE_KEYS = ("markdown", "documents", "code", "images", "other")
ALLOWED_MODES = {
    "semantic",
    "full",
    "extract",
    "md_only",
    "md_fallback",
    "catalog",
    "exclude",
}

PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "work_efficiency": {
        "label": "工作提效型",
        "short_label": "推荐",
        "description": "重点整理项目文档、交付资料和微信工作事实；代码正文只登记位置。",
        "rules": {
            "markdown": "semantic",
            "documents": "full",
            "code": "catalog",
            "images": "catalog",
            "other": "catalog",
        },
        "highlights": ["项目 Markdown 优先", "正式文档全文可搜", "代码只做目录索引"],
    },
    "complete_personal": {
        "label": "个人完整知识型",
        "short_label": "全面",
        "description": "更完整地整理个人资料、工作记录和聊天摘要，首次处理时间和空间占用更高。",
        "rules": {
            "markdown": "semantic",
            "documents": "semantic",
            "code": "catalog",
            "images": "extract",
            "other": "extract",
        },
        "highlights": ["个人资料覆盖更广", "重要文档直接进入语义检索", "图片先做本地摘要"],
    },
    "lightweight": {
        "label": "轻量快速型",
        "short_label": "省资源",
        "description": "先让系统快速可用，只处理项目说明和用户选择的正式资料。",
        "rules": {
            "markdown": "full",
            "documents": "extract",
            "code": "catalog",
            "images": "catalog",
            "other": "catalog",
        },
        "highlights": ["首次速度最快", "默认不扩大向量范围", "适合先试用再加深"],
    },
    "custom": {
        "label": "自定义方案",
        "short_label": "当前电脑",
        "description": "先让 AI 对文件做速判和摘要，再由用户确认哪些内容进入知识库。",
        "rules": {
            "markdown": "full",
            "documents": "full",
            "code": "catalog",
            "images": "catalog",
            "other": "catalog",
        },
        "highlights": ["AI 先给出用途和分类建议", "确认后生成 MD 并入库", "不自动扩大采集范围"],
    },
}


class ProcessingProfileUpdate(BaseModel):
    profile_id: Literal["work_efficiency", "complete_personal", "lightweight", "custom"]
    rules: dict[str, str] | None = None
    exclusions: list[str] = Field(default_factory=list, max_length=100)


def profile_path(settings: Settings) -> Path:
    return settings.data_root / "config" / "processing_profile.json"


def _validate_rules(rules: dict[str, str] | None) -> dict[str, str]:
    if rules is None:
        raise ValueError("自定义方案必须提供各类文件的处理方式")
    if set(rules) != set(RULE_KEYS):
        raise ValueError("处理方案必须包含 Markdown、文档、源码、图片和其他文件五类")
    normalized = {key: str(rules[key]) for key in RULE_KEYS}
    invalid = sorted(set(normalized.values()) - ALLOWED_MODES)
    if invalid:
        raise ValueError(f"处理方案包含不支持的处理方式：{', '.join(invalid)}")
    return normalized


def _payload(profile_id: str, rules: dict[str, str], exclusions: list[str]) -> dict[str, Any]:
    definition = PROFILE_DEFINITIONS[profile_id]
    return {
        "version": 1,
        "profile_id": profile_id,
        "rules": dict(rules),
        "exclusions": [item.strip() for item in exclusions if item.strip()],
        "label": definition["label"],
        "saved_by": "user_confirmed_profile",
    }


def load_processing_profile(settings: Settings) -> dict[str, Any]:
    """Load the current profile, safely defaulting this machine to custom."""

    path = profile_path(settings)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        profile_id = stored.get("profile_id", "custom")
        if profile_id not in PROFILE_IDS:
            raise ValueError("unknown profile")
        rules = stored.get("rules") or PROFILE_DEFINITIONS[profile_id]["rules"]
        rules = _validate_rules(rules)
        exclusions = stored.get("exclusions", [])
        if not isinstance(exclusions, list):
            exclusions = []
        return _view(profile_id, rules, exclusions, configured=True)
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return _view("custom", dict(PROFILE_DEFINITIONS["custom"]["rules"]), [], configured=False)


def _view(
    profile_id: str,
    rules: dict[str, str],
    exclusions: list[str],
    *,
    configured: bool,
) -> dict[str, Any]:
    definition = PROFILE_DEFINITIONS[profile_id]
    return {
        "profile_id": profile_id,
        "label": definition["label"],
        "short_label": definition["short_label"],
        "description": definition["description"],
        "selection_flow": (
            "ai_review_then_confirm" if profile_id == "custom" else "preset"
        ),
        "requires_ai_review": profile_id == "custom",
        "rules": dict(rules),
        "exclusions": list(exclusions),
        "highlights": list(definition["highlights"]),
        "configured": configured,
    }


def list_processing_profiles(settings: Settings) -> dict[str, Any]:
    current = load_processing_profile(settings)
    profiles = []
    for profile_id in PROFILE_IDS:
        definition = PROFILE_DEFINITIONS[profile_id]
        profiles.append(
            {
                "profile_id": profile_id,
                "label": definition["label"],
                "short_label": definition["short_label"],
                "description": definition["description"],
                "selection_flow": (
                    "ai_review_then_confirm" if profile_id == "custom" else "preset"
                ),
                "requires_ai_review": profile_id == "custom",
                "rules": dict(current["rules"] if profile_id == "custom" else definition["rules"]),
                "exclusions": list(current["exclusions"] if profile_id == "custom" else []),
                "highlights": list(definition["highlights"]),
                "selected": profile_id == current["profile_id"],
            }
        )
    return {
        "current": current,
        "profiles": profiles,
        "first_run_required": not current["configured"],
    }


def save_processing_profile(settings: Settings, request: ProcessingProfileUpdate) -> dict[str, Any]:
    if request.profile_id == "custom":
        rules = _validate_rules(request.rules)
    else:
        rules = dict(PROFILE_DEFINITIONS[request.profile_id]["rules"])
    exclusions = [item.strip() for item in request.exclusions if item.strip()]
    payload = _payload(request.profile_id, rules, exclusions)
    path = profile_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return load_processing_profile(settings)
