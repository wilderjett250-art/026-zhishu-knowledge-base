"""Bounded local inspection for every discovered file; never trusts its suffix alone."""

import codecs
import hashlib
import zipfile
from contextlib import suppress
from pathlib import Path
from xml.etree import ElementTree

from pkas.codex_capture import redact_secrets

MAGIC = {
    b"%PDF-": "pdf",
    b"PK\x03\x04": "zip_container",
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpeg",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"MZ": "executable",
}


def structured_sample(path: Path, detected: str) -> tuple[str, str, list[str]]:
    """Bound container size and individual decompressed XML. Never extract archive files."""
    if path.stat().st_size > 20_000_000:
        return detected, "", ["文件超过20MB：本轮仅签名检查，待分段处理"]
    parts = []
    if detected == "pdf":
        import fitz

        with fitz.open(stream=path.read_bytes(), filetype="pdf") as document:
            if document.needs_pass:
                return detected, "", ["PDF已加密，未读取正文"]
            pages = sorted({0, document.page_count // 2, document.page_count - 1})
            for page in pages:
                if page >= 0:
                    page_text = document[page].get_text("text")
                    if not isinstance(page_text, str):
                        page_text = str(page_text)
                    parts.append(f"第{page + 1}页：" + page_text[:1800])
            return detected, "\n".join(parts), [f"抽样{len(pages)}页/共{document.page_count}页"]
    if detected != "zip_container":
        return detected, "", []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) > 10000:
            return detected, "", ["压缩包条目过多，未读取正文"]
        selected = []
        if "word/document.xml" in names:
            detected, selected = "docx", ["word/document.xml"]
        elif "xl/workbook.xml" in names:
            detected = "xlsx"
            selected = ["xl/workbook.xml"]
            if "xl/sharedStrings.xml" in names:
                selected.append("xl/sharedStrings.xml")
            selected += sorted(n for n in names if n.startswith("xl/worksheets/")
                               and n.endswith(".xml"))
        elif "ppt/presentation.xml" in names:
            detected = "pptx"
            selected = sorted(n for n in names if n.startswith("ppt/slides/slide")
                              and n.endswith(".xml"))
        else:
            return detected, "", ["压缩容器已识别，内部文件未逐一审查"]
        notes = [f"结构抽样：最多32个XML部件；发现{len(selected)}个，未完整阅读"]
        budget = 8_000_000
        for name in selected[:32]:
            info = archive.getinfo(name)
            if info.file_size > min(2_000_000, budget) or info.flag_bits & 1:
                notes.append("存在过大或加密部件，待深读")
                continue
            raw = archive.read(name)
            budget -= len(raw)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                notes.append("存在外部实体声明，未解析")
                continue
            tree = ElementTree.fromstring(raw)
            values = []
            for node in tree.iter():
                tag = node.tag.rsplit("}", 1)[-1]
                if tag == "sheet":
                    values.append("工作表:" + node.attrib.get("name", ""))
                if tag in {"t", "v", "f"} and node.text:
                    values.append(node.text)
            parts.append(name + ": " + " ".join(values)[:2000])
        return detected, "\n".join(parts), notes


def inspect_file(path: Path, *, preview_bytes: int = 4096) -> dict:
    """Read a bounded prefix/structured sample and return evidence, not AI interpretation."""
    stat = path.stat()
    with path.open("rb") as stream:
        prefix = stream.read(preview_bytes)
    detected = next((name for magic, name in MAGIC.items() if prefix.startswith(magic)), "unknown")
    suffix = path.suffix.lower().lstrip(".") or "none"
    text = ""
    with suppress(UnicodeDecodeError):
        # A byte limit can cut through a multibyte Chinese character.
        text = codecs.getincrementaldecoder("utf-8-sig")().decode(prefix, final=False)
    text_like = bool(text and sum(c.isprintable() or c.isspace() for c in text) / len(text) > 0.85)
    if detected == "unknown" and text_like:
        detected = "text"
    notes = []
    if detected in {"pdf", "zip_container"}:
        try:
            detected, sample, notes = structured_sample(path, detected)
            if sample:
                text = sample
                text_like = True
        except Exception:
            # Do not leak parser exception text or mistake failure for a successful inspection.
            notes = ["结构解析失败，待人工核查或深读"]
            text, text_like = "", False
    aliases = {"jpeg": {"jpg", "jpeg"}, "executable": {"exe", "dll", "sys"},
               "zip_container": {"zip", "jar", "apk"}}
    accepted = aliases.get(detected, {detected})
    mismatch = detected not in {"unknown", "text"} and suffix not in accepted
    if detected == "text" and suffix in {"pdf", "docx", "xlsx", "png", "jpg", "exe"}:
        mismatch = True
    final_stat = path.stat()
    changed = (stat.st_size, stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns)
    if changed:
        text, text_like = "", False
        notes.append("读取期间文件发生变化，结果不可作为当前版本依据")
    return {
        "name": path.name,
        "suffix": suffix,
        "detected_type": detected,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "prefix_sha256": hashlib.sha256(prefix).hexdigest(),
        "preview_bytes": len(prefix),
        "text_preview": redact_secrets(text[:8000])[0] if text_like else None,
        "type_mismatch": mismatch,
        "inspection": "structured_sample_v2",
        "coverage": "partial" if text_like else "signature_only",
        "notes": notes,
        "changed_during_read": changed,
    }
