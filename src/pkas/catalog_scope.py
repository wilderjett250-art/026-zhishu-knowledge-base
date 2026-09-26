"""Cheap, reversible L0 gate for whole-machine file classification.

This policy only decides which catalogued paths deserve a bounded file read.
It never claims to understand their contents or promotes them into the KB.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pkas.ingest import SKIP_DIRECTORIES, is_sensitive_path
from pkas.processing_profiles import ALLOWED_MODES, PROFILE_DEFINITIONS, RULE_KEYS

POLICY_VERSION = 8
MARKDOWN_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".adoc"})
DOCUMENT_SUFFIXES = frozenset(
    {
        ".pdf", ".doc", ".docx", ".docm", ".odt", ".rtf",
        ".xls", ".xlsx", ".xlsm", ".ods", ".ppt", ".pptx",
        ".pptm", ".odp", ".txt", ".csv", ".tsv", ".json",
        ".jsonl", ".eml", ".msg", ".htm", ".html", ".epub",
    }
)
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"})
AUTO_SUFFIXES = MARKDOWN_SUFFIXES | DOCUMENT_SUFFIXES | IMAGE_SUFFIXES
GENERATED_PARTS = frozenset(
    SKIP_DIRECTORIES | {
        ".git", ".svn", ".hg", "node_modules", ".venv", "venv",
        "__pycache__", "site-packages", "bower_components", ".gradle",
        ".m2", ".next", ".nuxt", "cmakefiles", "dist", "build",
        "target", "vendor", "third_party", "third-party", ".cache",
        "backups", "backup", "recovery", "pkas-recovery", "pkas-archives",
        "codex-backups", "pkas-builds", "pkas-build-cache",
        ".pytest_cache", ".tox", "windows", "program files",
        "program files (x86)", "programdata", "$recycle.bin",
        "system volume information", ".ssh",
    }
)
TRANSIENT_SUFFIXES = frozenset({".cache", ".err", ".lock", ".log", ".tmp"})


def load_auto_policy(data_root: Path) -> dict[str, Any]:
    fallback = PROFILE_DEFINITIONS["custom"]["rules"]
    path = data_root / "config" / "processing_profile.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        rules = stored["rules"]
        if (
            not isinstance(rules, dict)
            or set(rules) != set(RULE_KEYS)
            or any(value not in ALLOWED_MODES for value in rules.values())
        ):
            raise ValueError("invalid rules")
        raw_exclusions = stored.get("exclusions", [])
        if not isinstance(raw_exclusions, list):
            raise ValueError("invalid exclusions")
        exclusions = sorted(
            {value.casefold() for value in raw_exclusions if isinstance(value, str) and value}
        )
        return {"rules": dict(rules), "exclusions": exclusions}
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {"rules": dict(fallback), "exclusions": []}


def policy_signature(policy: dict[str, Any]) -> str:
    payload = json.dumps(
        {"version": POLICY_VERSION, "policy": policy}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def candidate_priority(
    path: str, extension: str, policy: dict[str, Any], byte_size: int | None = None
) -> tuple[int | None, str]:
    """Return an inspection priority or an explicit metadata-only L0 reason.

    This is a file *selection* rule, not a content classification. A user can
    still select any L0 path manually if the cheap gate missed useful content.
    """
    suffix = extension.casefold()
    if suffix in TRANSIENT_SUFFIXES:
        return None, "transient"
    parts = {part.casefold() for part in re.split(r"[\\/]", path) if part}
    if byte_size == 0:
        return None, "empty_file"
    if Path(path).name.casefold() in {"package-lock.json", "npm-shrinkwrap.json"}:
        return None, "generated_manifest"
    if suffix == ".txt" and parts & {"train", "val", "labels"}:
        return None, "training_label_or_split"
    if parts & (GENERATED_PARTS | set(policy["exclusions"])):
        return None, "generated_or_excluded_directory"
    if is_sensitive_path(Path(path)):
        return None, "sensitive_path"
    if suffix in MARKDOWN_SUFFIXES:
        kind = "markdown"
    elif suffix in DOCUMENT_SUFFIXES:
        kind = "documents"
    elif suffix in IMAGE_SUFFIXES:
        kind = "images"
    else:
        return None, "unsupported_or_code"
    rule = policy["rules"].get(kind)
    if rule in {"catalog", "exclude"} or (rule == "md_only" and kind != "markdown"):
        return None, "profile_l0"
    if suffix == ".md":
        return 0, "inspect_original"
    if suffix in MARKDOWN_SUFFIXES | {
        ".pdf", ".doc", ".docx", ".xlsx", ".xls", ".ppt", ".pptx",
    }:
        return 1, "inspect_original"
    if suffix in {".txt", ".eml", ".msg", ".rtf", ".odt"}:
        return 2, "inspect_original"
    if suffix in IMAGE_SUFFIXES:
        return 4, "inspect_metadata_first"
    return 3, "inspect_original"
