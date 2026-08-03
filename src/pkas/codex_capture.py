import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from pkas.config import Settings
from pkas.ingest import IngestionService

MAX_CAPTURE_CHARS = 200_000

_AMBIENT_PROMPT_MARKER = (
    "you are an expert at upholding safety and compliance standards "
    "for codex ambient suggestions"
)

_SECRET_PATTERNS = (
    (
        re.compile(
            r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
            re.IGNORECASE | re.DOTALL,
        ),
        "[REDACTED]",
    ),
    (
        re.compile(
            r"data:(?:image|audio|video|application)/[^;\s]+;base64,[A-Za-z0-9+/=_-]+",
            re.I,
        ),
        "[REDACTED]",
    ),
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
        ),
        "[REDACTED]",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "[REDACTED]",
    ),
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(password|passwd|token|api[_-]?key|secret|authorization|cookie)"
            r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s/@]+)(@)"),
        r"\1[REDACTED]\3",
    ),
)


def redact_secrets(text: str) -> tuple[str, int]:
    redacted = text
    replacements = 0
    for pattern, replacement in _SECRET_PATTERNS:
        redacted, count = pattern.subn(replacement, redacted)
        replacements += count
    return redacted, replacements


def is_internal_codex_turn(*, user_text: str, cwd: str | None) -> bool:
    """Identify hidden Codex desktop ambient-suggestion runs, not user tasks."""
    normalized_cwd = (cwd or "").replace("/", "\\").lower()
    return (
        _AMBIENT_PROMPT_MARKER in user_text[:2_000].lower()
        and "\\windowsapps\\openai.codex_" in normalized_cwd
    )


def _text_from_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_text_from_value(item) for item in value]
        return "\n".join(part for part in parts if part.strip())
    if isinstance(value, dict):
        for key in ("text", "message", "content", "input_text", "output_text"):
            if key in value:
                text = _text_from_value(value[key])
                if text.strip():
                    return text
        return ""
    return ""


def _bounded(text: str) -> tuple[str, bool]:
    clean = text.replace("\x00", "").strip()
    if len(clean) <= MAX_CAPTURE_CHARS:
        return clean, False
    return f"{clean[:MAX_CAPTURE_CHARS]}\n\n[内容过长，已截断]", True


def build_codex_turn(
    *,
    thread_id: str,
    turn_id: str,
    user_text: str,
    assistant_text: str,
    cwd: str | None = None,
    thread_name: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    capture_mode: str = "notify",
) -> dict[str, Any]:
    bounded_user, user_truncated = _bounded(user_text)
    bounded_assistant, assistant_truncated = _bounded(assistant_text)
    safe_user, user_redactions = redact_secrets(bounded_user)
    safe_assistant, assistant_redactions = redact_secrets(bounded_assistant)
    safe_cwd, cwd_redactions = redact_secrets(cwd or "")

    first_line = next((line.strip() for line in safe_user.splitlines() if line.strip()), "")
    title = (thread_name or first_line or f"Codex 任务 {turn_id}").strip()[:120]
    header = [
        "# Codex 任务记录",
        "",
        f"- 任务标题：{title}",
        f"- thread_id：{thread_id}",
        f"- turn_id：{turn_id}",
    ]
    if safe_cwd:
        header.append(f"- 工作目录：{safe_cwd}")
    if started_at:
        header.append(f"- 开始时间：{started_at}")
    if completed_at:
        header.append(f"- 完成时间：{completed_at}")
    body = [
        *header,
        "",
        "## 用户请求",
        "",
        safe_user or "（本轮没有可索引的文字请求）",
        "",
        "## Codex 最终回答",
        "",
        safe_assistant or "（本轮没有可索引的最终回答）",
    ]
    return {
        "title": title,
        "text": "\n".join(body).strip(),
        "original_uri": f"codex://{thread_id}/{turn_id}",
        "event_time": completed_at or started_at,
        "metadata": {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "cwd": safe_cwd or None,
            "thread_name": thread_name,
            "started_at": started_at,
            "completed_at": completed_at,
            "capture_mode": capture_mode,
            "redaction_count": user_redactions + assistant_redactions + cwd_redactions,
            "user_truncated": user_truncated,
            "assistant_truncated": assistant_truncated,
        },
    }


def capture_notification(
    payload: dict[str, Any],
    *,
    settings: Settings | None = None,
    ingestion: IngestionService | None = None,
) -> dict[str, Any]:
    if payload.get("type") != "agent-turn-complete":
        return {"status": "ignored", "reason": "unsupported_event"}

    thread_id = str(payload.get("thread-id") or payload.get("thread_id") or "unknown-thread")
    turn_id = str(payload.get("turn-id") or payload.get("turn_id") or "unknown-turn")
    user_text = _text_from_value(payload.get("input-messages") or payload.get("input_messages"))
    assistant_text = _text_from_value(
        payload.get("last-assistant-message") or payload.get("last_assistant_message")
    )
    cwd = _text_from_value(payload.get("cwd"))
    if not user_text.strip() and not assistant_text.strip():
        return {"status": "ignored", "reason": "empty_turn"}
    if is_internal_codex_turn(user_text=user_text, cwd=cwd):
        return {"status": "ignored", "reason": "internal_codex_turn"}

    turn = build_codex_turn(
        thread_id=thread_id,
        turn_id=turn_id,
        user_text=user_text,
        assistant_text=assistant_text,
        cwd=cwd,
        capture_mode="notify",
    )
    service = ingestion or IngestionService(settings=settings)
    return service.import_text(
        text=turn["text"],
        title=turn["title"],
        original_uri=turn["original_uri"],
        source_type="codex-turn",
        domain="work",
        privacy="private",
        metadata=turn["metadata"],
        event_time=turn["event_time"],
        source_created_at=turn["event_time"],
    )


def _run_delegate(executable: str | None, arguments: list[str], raw_payload: str) -> None:
    if not executable or not Path(executable).is_file():
        return
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            [executable, *arguments, raw_payload],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--delegate-exe")
    parser.add_argument("--delegate-arg", action="append", default=[])
    parser.add_argument("payload", nargs="?")
    args, extras = parser.parse_known_args()
    raw_payload = args.payload or (extras[-1] if extras else "")
    if not raw_payload and not sys.stdin.isatty():
        raw_payload = sys.stdin.read()

    _run_delegate(args.delegate_exe, args.delegate_arg, raw_payload)
    try:
        payload = json.loads(raw_payload)
        if isinstance(payload, dict):
            capture_notification(payload)
    except (json.JSONDecodeError, OSError, ValueError):
        return


if __name__ == "__main__":
    main()
