"""Two-level, versioned content categories. Classification is a reviewable suggestion."""

import hashlib
import json
import os
import threading
import uuid
from copy import deepcopy
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

LOCK = threading.RLock()


class ContentCategory(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=40)
    parent_id: str | None = None
    keywords: list[str] = Field(default_factory=list, max_length=30)


class TaxonomyUpdate(BaseModel):
    expected_revision: str
    categories: list[ContentCategory] = Field(min_length=2, max_length=200)

    @model_validator(mode="after")
    def validate_tree(self):
        by_id = {c.id: c for c in self.categories}
        if len(by_id) != len(self.categories):
            raise ValueError("分类编号不能重复")
        names = set()
        for c in self.categories:
            if not c.name.strip() or any(not k.strip() or len(k) > 80 for k in c.keywords):
                raise ValueError("分类名称及关键词不能为空，关键词最长80字")
            identity = (c.parent_id, c.name.strip().casefold())
            if identity in names:
                raise ValueError("同一级内分类名称不能重复")
            names.add(identity)
            if c.parent_id is not None:
                parent = by_id.get(c.parent_id)
                if parent is None or parent.id == c.id or parent.parent_id is not None:
                    raise ValueError("只允许两级分类，子类必须属于一个一级分类")
        if "unresolved" not in by_id or by_id["unresolved"].parent_id is not None:
            raise ValueError("必须保留待判断一级分类")
        if "unresolved_other" not in by_id or by_id["unresolved_other"].parent_id != "unresolved":
            raise ValueError("必须保留待判断子类")
        return self


class TaxonomyRestore(BaseModel):
    expected_revision: str


DEFAULT_GROUPS = [
    ("work", "工作业务", [
        ("requirements", "需求与方案", ["功能需求", "验收标准", "需求说明", "需求分析"]),
        ("delivery", "交付与运营", ["交付清单", "运营方案", "实施方案"]),
        ("business", "商务与客户", ["报价单", "客户需求", "商务合作"]),
        ("management", "管理与会议", ["会议纪要", "行动事项", "会议议程"])]),
    ("engineering", "软件与工程", [
        ("source", "源码与开发", ["def __init__", "import react", "public class", "#include"]),
        ("deployment", "架构与部署", ["docker compose", "部署步骤", "架构设计", "nginx"]),
        ("hardware", "硬件与嵌入式", ["原理图", "gpio", "stm32", "esp32"]),
        ("data", "数据与模型", ["训练集", "模型训练", "数据集", "训练参数"])]),
    ("learning", "学习研究", [
        ("course", "课程与笔记", ["学习笔记", "课程大纲", "课后练习"]),
        ("research", "论文与研究", ["参考文献", "研究方法", "abstract", "references"]),
        ("exam", "考试与竞赛", ["考试大纲", "竞赛规则", "模拟试题"])]),
    ("personal", "个人生活", [
        ("identity", "履历与证书", ["教育经历", "工作经历", "获奖证书"]),
        ("journal", "日记与计划", ["个人日记", "每日计划", "生活记录"]),
        ("life", "生活与健康", ["体检报告", "旅行行程", "用药说明"])]),
    ("finance", "财务法务", [
        ("billing", "账单与票据", ["增值税", "发票号码", "银行流水", "报销单"]),
        ("contract", "合同与协议", ["甲方", "乙方", "违约责任", "合同编号"]),
        ("legal", "法规与合规", ["法律法规", "合规要求", "法律意见"])]),
    ("communication", "沟通记录", [
        ("chat", "聊天记录", ["发送时间", "发送人", "聊天记录", "消息类型"]),
        ("mail", "邮件", ["mime-version:", "message-id:", "邮件主题"]),
        ("transcript", "通话与转写", ["通话转写", "说话人", "录音转写"])]),
    ("media", "媒体素材", [
        ("image", "图片与设计", []), ("av", "音视频", []),
        ("creative", "文案与素材", ["分镜脚本", "品牌文案", "拍摄脚本"])]),
    ("software", "软件与系统", [
        ("installer", "程序与安装包", []),
        ("config", "配置与日志", ["traceback (most recent call last)", "stack trace"])]),
    ("archive", "备份归档", [
        ("package", "压缩与打包", []),
        ("backup", "备份说明", ["备份时间", "恢复步骤", "备份清单"])]),
    ("unresolved", "待判断", [("other", "信息不足或用途不明确", [])]),
]


def defaults() -> list[dict]:
    result = []
    for group, name, children in DEFAULT_GROUPS:
        result.append(dict(id=group, name=name, parent_id=None, keywords=[]))
        for child, label, words in children:
            result.append(dict(id=f"{group}_{child}", name=label, parent_id=group, keywords=words))
    return result


def revision(categories: list[dict]) -> str:
    return hashlib.sha256(json.dumps(categories, sort_keys=True).encode()).hexdigest()


def system_taxonomy() -> dict:
    categories = defaults()
    return {"revision": revision(categories), "categories": categories}


def load_taxonomy(data_root: Path) -> dict:
    path = data_root / "config" / "content-taxonomy.json"
    raw_categories: object = (
        json.loads(path.read_text(encoding="utf-8")) if path.exists() else defaults()
    )
    validated = TaxonomyUpdate.model_validate(
        {"expected_revision": "", "categories": raw_categories}
    )
    categories = [c.model_dump() for c in validated.categories]
    baseline = system_taxonomy()
    current_revision = revision(categories)
    return {
        "revision": current_revision,
        "categories": categories,
        "system_revision": baseline["revision"],
        "customized": current_revision != baseline["revision"],
        "system_category_count": len(baseline["categories"]),
    }


def _preserve_current(path: Path) -> None:
    if not path.exists():
        return
    previous = path.read_bytes()
    backup = path.with_suffix(".previous.json")
    atomic_write(backup, previous)
    if backup.read_bytes() != previous:
        raise OSError("分类配置恢复点验证失败")


def save_taxonomy(data_root: Path, update: TaxonomyUpdate) -> dict:
    with LOCK:
        current = load_taxonomy(data_root)
        if current["revision"] != update.expected_revision:
            raise ValueError("分类已被修改，请刷新后重试")
        path = data_root / "config" / "content-taxonomy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep one exact predecessor. Existing plans retain their own category snapshots.
        _preserve_current(path)
        atomic_write(path, json.dumps([c.model_dump() for c in update.categories],
                                    ensure_ascii=False).encode("utf-8"))
        return load_taxonomy(data_root)


def restore_system_taxonomy(data_root: Path, request: TaxonomyRestore) -> dict:
    """Restore the immutable product baseline while preserving the user's prior copy."""
    with LOCK:
        current = load_taxonomy(data_root)
        if current["revision"] != request.expected_revision:
            raise ValueError("分类已被修改，请刷新后重试")
        path = data_root / "config" / "content-taxonomy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _preserve_current(path)
        atomic_write(
            path,
            json.dumps(system_taxonomy()["categories"], ensure_ascii=False).encode("utf-8"),
        )
        return load_taxonomy(data_root)


def atomic_write(path: Path, value: bytes):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def classify(record: dict, taxonomy: dict) -> dict:
    text = (record.get("text_preview") or "").casefold()
    candidates = []
    for c in taxonomy["categories"]:
        if not c["parent_id"] or c["id"] == "unresolved_other":
            continue
        hits = [w for w in c["keywords"] if w.casefold() in text]
        if hits:
            candidates.append({"category_id": c["id"], "evidence": hits, "score": len(hits)})
    candidates.sort(key=lambda c: (-c["score"], c["category_id"]))
    ambiguous = len(candidates) > 1 and candidates[0]["score"] == candidates[1]["score"]
    selected = candidates[0]["category_id"] if candidates and not ambiguous else "unresolved_other"
    evidence = candidates[0]["evidence"] if selected != "unresolved_other" else []
    basis = "content_keywords" if evidence else "insufficient_content"
    # A format fallback describes only the container; no unsupported business meaning.
    fallback = {"png": "media_image", "jpeg": "media_image", "gif": "media_image",
                "zip_container": "archive_package", "executable": "software_installer"}
    known = {c["id"]: c for c in taxonomy["categories"]}
    if not candidates and record.get("detected_type") in fallback:
        option = fallback[record["detected_type"]]
        if option in known:
            selected, basis = option, "format_only"
            evidence = [record["detected_type"]]
    if basis == "format_only":
        confidence = 0.25
        review_reason = "只有文件格式依据，不能证明实际用途"
    elif ambiguous:
        confidence = 0.2
        review_reason = "多个分类命中同等证据，必须复核"
    elif not candidates:
        confidence = 0.0
        review_reason = "没有足够正文证据，必须复核或保持待判断"
    else:
        top_score = candidates[0]["score"]
        next_score = candidates[1]["score"] if len(candidates) > 1 else 0
        confidence = min(0.85, 0.35 + top_score * 0.15 + max(0, top_score - next_score) * 0.1)
        review_reason = "本地关键词初判；有正文时仍需AI复核"
    return {"category_id": selected, "parent_id": known[selected]["parent_id"],
            "label": known[selected]["name"], "basis": basis, "evidence": evidence,
            "review_status": "suggested" if selected != "unresolved_other" else "needs_review",
            "candidates": deepcopy(candidates[:3]), "taxonomy_revision": taxonomy["revision"],
            "method": "local_content_rules_v1", "ambiguous": ambiguous,
            "confidence": round(confidence, 3), "review_reason": review_reason,
            "needs_ai_review": bool(record.get("text_preview")) and basis != "format_only"}
