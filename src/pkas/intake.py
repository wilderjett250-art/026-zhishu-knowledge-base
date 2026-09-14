"""Explicit, bounded local intake. No schedules, LLM calls or original copies."""

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from pkas.codex_capture import redact_secrets
from pkas.everything_scanner import scan as everything_scan
from pkas.ingest import (
    SKIP_DIRECTORIES,
    IngestionService,
    chunk_document,
    is_sensitive_path,
    sha256_file,
)
from pkas.local_lock import WindowsFileLock
from pkas.parsers import SUPPORTED_EXTENSIONS, TEXT_EXTENSIONS, ParseError, parse_file

Mode = Literal[
    "semantic",
    "full",
    "extract",
    "md_only",
    "md_fallback",
    "catalog",
    "exclude",
]
EXCLUDED = SKIP_DIRECTORIES | {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "appdata",
    "$recycle.bin",
    "system volume information",
    "recovery",
    ".ssh",
}
DEFAULT_RULES = {
    "markdown": "full",
    "documents": "full",
    "code": "catalog",
    "images": "catalog",
    "other": "catalog",
}


class IntakeRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    selected_entries: list[str] = Field(default_factory=list, max_length=500)
    rules: dict[str, Mode] = Field(default_factory=lambda: dict(DEFAULT_RULES))
    exclusions: list[str] = Field(default_factory=list, max_length=50)
    max_files: int = Field(default=2000, ge=1, le=10000)
    scanner: Literal["native", "everything"] = "native"


class IntakeBrowseRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


class IntakePolicy(BaseModel):
    rules: dict[str, Mode]


class IntakeItemError(ValueError):
    """Only fixed, non-sensitive messages authored here may reach the UI."""


def category(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "markdown"
    if suffix in {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".csv",
        ".ppt",
        ".pptx",
        ".txt",
        ".json",
        ".jsonl",
        ".eml",
        ".html",
        ".epub",
    }:
        return "documents"
    if suffix in TEXT_EXTENSIONS:
        return "code"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
        return "images"
    return "other"


def linked(path: Path) -> bool:
    return any(
        p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
        for p in [path, *path.parents]
    )


class IntakeService:
    def __init__(self, system):
        self.system = system
        self.settings = system.settings.model_copy(
            update={
                "document_ai_enhancement_enabled": False,
                "document_docling_enabled": False,
                "document_paddleocr_enabled": False,
                "document_allow_remote_processing": False,
                "document_allow_restricted_remote_processing": False,
            }
        )
        self.home = self.settings.data_root / "intake"
        self.guard = threading.RLock()
        self.thread = None
        self.active = None
        self.stop = threading.Event()

    def _path(self, job_id):
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("无效任务编号")
        return self.home / f"{job_id}.json"

    def _save(self, plan):
        self.home.mkdir(parents=True, exist_ok=True)
        path = self._path(plan["id"])
        temporary = path.with_suffix(".tmp")
        with self.guard:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(plan, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)

    def read(self, job_id):
        with self.guard:
            plan = json.loads(self._path(job_id).read_text(encoding="utf-8"))
        if plan["state"] in {"scanning", "running", "backing_up"} and self.active != job_id:
            plan["state"] = "interrupted"
        return plan

    def view(self, job_id, offset=0):
        plan = self.read(job_id)
        items = plan.pop("items")
        plan["counts"] = dict(Counter(i["state"] for i in items))
        plan["total"] = len(items)
        plan["categories"] = {
            key: {
                "count": sum(i["category"] == key for i in items),
                "bytes": sum(i["bytes"] for i in items if i["category"] == key),
            }
            for key in DEFAULT_RULES
        }
        plan["items"] = items[offset : offset + 100]
        plan["offset"] = offset
        plan["has_more"] = offset + 100 < len(items)
        return plan

    def recent(self):
        if not self.home.exists():
            return []
        result = []
        for path in sorted(self.home.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[
            :20
        ]:
            value = self.view(path.stem)
            value.pop("items")
            result.append(value)
        return result

    def _authorize_root(self, value: str) -> Path:
        raw = Path(value).expanduser()
        if not raw.is_absolute() or str(raw).startswith(("\\\\", "//")) or linked(raw):
            raise ValueError("请选择本地绝对路径，不支持网络目录或符号链接")
        root = raw.resolve(strict=True)
        if not root.is_dir() or any(p.casefold() in EXCLUDED for p in root.parts):
            raise ValueError("请选择普通资料目录，系统和缓存目录不能接入")
        data = self.settings.data_root.resolve()
        if root == data or data in root.parents:
            raise ValueError("不能把知识库自身数据再次接入")
        return root

    def browse(self, request: IntakeBrowseRequest) -> dict:
        """List direct children only; recursive discovery waits for explicit selection."""
        root = self._authorize_root(request.path)
        data = self.settings.data_root.resolve()
        items = []
        with os.scandir(root) as entries:
            for entry in entries:
                path = Path(entry.path)
                blocked = (
                    entry.name.casefold() in EXCLUDED
                    or is_sensitive_path(path)
                    or linked(path)
                    or path.resolve() == data
                    or data in path.resolve().parents
                )
                kind = "directory" if entry.is_dir(follow_symlinks=False) else "file"
                if kind == "file" and not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                items.append(
                    {
                        "name": entry.name,
                        "path": str(path),
                        "kind": kind,
                        "bytes": stat.st_size if kind == "file" else None,
                        "modified_ns": stat.st_mtime_ns,
                        "selectable": not blocked,
                        "reason": "系统、缓存、密钥或链接范围" if blocked else None,
                    }
                )
                if len(items) > 5000:
                    raise ValueError("一级目录条目超过5000个，请选择更具体的目录")
        items.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold()))
        return {
            "root": str(root),
            "items": items,
            "selectable_count": sum(bool(item["selectable"]) for item in items),
            "provider": "windows_level1",
            "recursive_provider": "everything_after_selection",
            "note": "这里只读取第一层名称和元数据；勾选后才由Everything递归扫描所选范围。",
        }

    def _launch(self, job_id, fn, *args):
        with self.guard:
            if self.thread and self.thread.is_alive():
                raise ValueError("已有分类接入任务运行，请等待或先取消")
            self.stop.clear()
            self.active = job_id

            def work():
                try:
                    fn(*args)
                except Exception as exc:
                    plan = self.read(job_id)
                    plan["state"] = "failed"
                    plan["error"] = f"任务失败（{type(exc).__name__}），请检查空间或重新预览"
                    self._save(plan)
                finally:
                    self.active = None

            self.thread = threading.Thread(target=work, name="pkas-intake", daemon=True)
            self.thread.start()

    def preview(self, request: IntakeRequest):
        root = self._authorize_root(request.path)
        if set(request.rules) != set(DEFAULT_RULES):
            raise ValueError("请为五种资料分类设置处理方式")
        if any(
            "/" in s or "\\" in s or s in {".", ".."} or not s.strip() for s in request.exclusions
        ):
            raise ValueError("排除项请填写目录名，不是路径或通配符")
        if len(set(request.selected_entries)) != len(request.selected_entries):
            raise ValueError("勾选范围不能重复")
        for name in request.selected_entries:
            if (
                not name.strip()
                or name in {".", ".."}
                or "/" in name
                or "\\" in name
                or Path(name).name != name
            ):
                raise ValueError("只能勾选当前目录第一层条目")
            selected = (root / name).resolve(strict=True)
            if selected.parent != root or linked(selected) or is_sensitive_path(selected):
                raise ValueError("勾选项已失效或属于不可接入范围")
        plan = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(UTC).isoformat(),
            "root": str(root),
            "allowed_roots": [str(root)],
            "request": request.model_dump(),
            "state": "scanning",
            "items": [],
            "scan_complete": False,
            "excluded_directories": 0,
            "cloud_called": False,
            "original_copy_bytes": 0,
            "recovery_bytes_estimate": self.settings.database_path.stat().st_size,
            "note": "原地引用；本地摘录不是AI总结。向量仅排队，不自动调用API。",
            "scanner": request.scanner,
            "classification_basis": "扩展名规则，不代表已理解正文或项目用途",
        }
        with self.guard:
            if self.thread and self.thread.is_alive():
                raise ValueError("已有任务运行")
            self._save(plan)
            self._launch(plan["id"], self._scan, plan)
        return self.view(plan["id"])

    def preview_classified(self, selection: dict):
        """Build an intake plan from a completed classification manifest without rescanning."""
        mode = selection.get("mode")
        if mode not in {"catalog", "full", "semantic"}:
            raise ValueError("处理方式无效")
        roots = []
        for value in selection.get("roots", []):
            root = Path(value).expanduser()
            if not root.is_absolute() or str(root).startswith(("\\\\", "//")) or linked(root):
                raise ValueError("分类任务中的资料范围已失效")
            resolved = root.resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError("分类任务中的资料范围已失效")
            roots.append(resolved)
        if not roots:
            raise ValueError("分类任务没有可复用的资料范围")
        data_root = self.settings.data_root.resolve()
        items = []
        for source in selection.get("items", []):
            item = {
                "path": str(source.get("path", "")),
                "relative": str(source.get("relative") or source.get("path", "")),
                "category": "other",
                "content_category_id": source.get("content_category_id"),
                "content_category_label": source.get("content_category_label"),
                "classification_basis": source.get("classification_basis"),
                "classification_revision": source.get("classification_revision"),
                "summary_file_id": source.get("summary_file_id"),
                "bytes": int(source.get("bytes", 0)),
                "mtime_ns": int(source.get("mtime_ns", 0)),
                "action": mode,
                "state": "pending",
            }
            try:
                path = Path(item["path"])
                resolved = path.resolve(strict=True)
                if (
                    not path.is_absolute()
                    or linked(path)
                    or is_sensitive_path(path)
                    or resolved == data_root
                    or data_root in resolved.parents
                    or not any(root in resolved.parents for root in roots)
                ):
                    raise OSError("unsafe")
                stat = resolved.stat()
                if stat.st_size != item["bytes"] or stat.st_mtime_ns != item["mtime_ns"]:
                    item.update(state="skipped", reason="文件在分类后发生变化，请先增量复查")
                else:
                    item["path"] = str(resolved)
                    item["category"] = category(resolved)
                    if mode in {"full", "semantic"} and (
                        resolved.suffix.lower() not in SUPPORTED_EXTENSIONS
                        or stat.st_size > min(self.settings.max_source_bytes, 20_000_000)
                    ):
                        item.update(state="skipped", reason="当前格式不支持正文解析或超过20MB")
            except OSError:
                item.update(state="skipped", reason="文件不可访问或已移出原分类范围")
            items.append(item)
        if not items:
            raise ValueError("所选分类中没有文件")
        plan = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(UTC).isoformat(),
            "root": selection.get("root") or "已分类资料",
            "allowed_roots": [str(root) for root in roots],
            "request": {
                "source": "summary_job",
                "source_summary_job": selection["source_summary_job"],
                "category_ids": selection["category_ids"],
                "mode": mode,
            },
            "state": "ready",
            "items": items,
            "scan_complete": True,
            "excluded_directories": 0,
            "cloud_called": False,
            "original_copy_bytes": 0,
            "recovery_bytes_estimate": self.settings.database_path.stat().st_size,
            "source_bytes": sum(item["bytes"] for item in items),
            "actions": dict(Counter(item["action"] for item in items)),
            "note": "复用已保存分类清单；执行前再次校验原文件。AI分类仅用于筛选，不作为正文入库。",
            "scanner": "saved_classification_manifest",
            "classification_basis": "用户选择的用途分类；原文件仍是唯一正文来源",
        }
        with self.guard:
            if self.thread and self.thread.is_alive():
                raise ValueError("已有分类接入任务运行")
            self._save(plan)
        return self.view(plan["id"])

    def _scan(self, plan):
        if plan["request"].get("scanner") == "everything":
            return self._scan_everything(plan)
        root = Path(plan["root"])
        rules = plan["request"]["rules"]
        excluded = EXCLUDED | {s.casefold() for s in plan["request"]["exclusions"]}
        selected = {s.casefold() for s in plan["request"].get("selected_entries", [])}
        stack = [root]
        started = time.monotonic()
        visited = 0
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if directory == root and selected and entry.name.casefold() not in selected:
                            continue
                        visited += 1
                        if self.stop.is_set():
                            plan["state"] = "cancelled"
                            self._save(plan)
                            return
                        if (
                            len(plan["items"]) >= plan["request"]["max_files"]
                            or visited > 30000
                            or time.monotonic() - started > 30
                        ):
                            plan["state"] = "limited"
                            plan["error"] = "预览达到数量/30秒上限，未完整扫描；请缩小目录后重试"
                            self._save(plan)
                            return
                        path = Path(entry.path)
                        if (
                            entry.name.casefold() in excluded
                            or is_sensitive_path(path)
                            or linked(path)
                            or path.resolve() == self.settings.data_root.resolve()
                        ):
                            plan["excluded_directories"] += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        kind = category(path)
                        action = rules[kind]
                        if action == "md_only":
                            action = "full" if path.suffix.lower() == ".md" else "exclude"
                        item = {
                            "path": str(path),
                            "relative": str(path.relative_to(root)),
                            "category": kind,
                            "action": action,
                            "bytes": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                            "state": "pending",
                        }
                        if action == "exclude":
                            item["state"] = "excluded"
                        elif action in {"full", "extract"} and (
                            path.suffix.lower() not in SUPPORTED_EXTENSIONS
                            or stat.st_size > min(self.settings.max_source_bytes, 20_000_000)
                        ):
                            item.update(state="skipped", reason="不支持解析或超过本次20MB上限")
                        plan["items"].append(item)
                        if len(plan["items"]) % 100 == 0:
                            self._save(plan)
            except OSError:
                plan["state"] = "failed"
                plan["error"] = "有目录无法读取，未完整扫描；请缩小范围或调整排除项"
                self._save(plan)
                return
        # MD fallback is scoped to this chosen root, not a guess about project boundaries.
        has_md = any(
            Path(i["path"]).suffix.lower() == ".md" and i["state"] != "excluded"
            for i in plan["items"]
        )
        for item in plan["items"]:
            if item["action"] == "md_fallback":
                item["action"] = (
                    "full"
                    if Path(item["path"]).suffix.lower() == ".md"
                    else "catalog"
                    if has_md
                    else "map"
                )
                if item["action"] == "full" and item["bytes"] > 20_000_000:
                    item.update(state="skipped", reason="超过本次20MB上限")
        plan.update(
            state="ready",
            scan_complete=True,
            source_bytes=sum(i["bytes"] for i in plan["items"]),
            actions=dict(Counter(i["action"] for i in plan["items"])),
        )
        self._save(plan)

    def _scan_everything(self, plan):
        root = Path(plan["root"])
        excluded = EXCLUDED | {s.casefold() for s in plan["request"]["exclusions"]}
        try:
            names = plan["request"].get("selected_entries", [])
            scopes = [root / name for name in names] if names else [root]
            for index, scope in enumerate(scopes):
                candidates = (
                    iter([scope])
                    if scope.is_file()
                    else everything_scan(
                        self.settings.project_root,
                        scope,
                        self.home / f"{plan['id']}-{index}.efu",
                        excluded,
                        self.stop,
                    )
                )
                with closing(candidates):
                    for path in candidates:
                        if self.stop.is_set():
                            plan["state"] = "cancelled"
                            self._save(plan)
                            return
                        if len(plan["items"]) >= plan["request"]["max_files"]:
                            plan.update(state="limited", error="文件超过本批上限，请减少勾选范围")
                            self._save(plan)
                            return
                        data = self.settings.data_root.resolve()
                        if (
                            any(p.casefold() in excluded for p in path.parts)
                            or is_sensitive_path(path)
                            or linked(path)
                            or data == path.resolve()
                            or data in path.resolve().parents
                        ):
                            plan["excluded_directories"] += 1
                            continue
                        stat = path.stat()
                        if not path.is_file():
                            continue
                        plan["items"].append(
                            {
                                "path": str(path),
                                "relative": str(path.relative_to(root)),
                                "category": category(path),
                                "bytes": stat.st_size,
                                "mtime_ns": stat.st_mtime_ns,
                                "action": "catalog",
                                "state": "pending",
                            }
                        )
            plan.update(
                state="ready",
                scan_complete=True,
                source_bytes=sum(i["bytes"] for i in plan["items"]),
            )
            self._set_rules(plan, plan["request"]["rules"])
            self._save(plan)
        except (OSError, ValueError) as exc:
            plan.update(
                state="cancelled" if self.stop.is_set() else "failed",
                error="Everything 扫描未完成，请检查权限/组件或缩小范围",
                error_code=type(exc).__name__,
            )
            self._save(plan)

    def _set_rules(self, plan, rules):
        if set(rules) != set(DEFAULT_RULES):
            raise ValueError("请为所有分类设置处理方式")
        for item in plan["items"]:
            item.pop("reason", None)
            action = rules[item["category"]]
            suffix = Path(item["path"]).suffix.lower()
            if action == "md_only":
                action = "full" if suffix == ".md" else "exclude"
            item.update(action=action, state="excluded" if action == "exclude" else "pending")
        has_md = any(
            Path(i["path"]).suffix.lower() == ".md" and i["state"] != "excluded"
            for i in plan["items"]
        )
        for item in plan["items"]:
            suffix = Path(item["path"]).suffix.lower()
            if item["action"] == "md_fallback":
                item["action"] = "full" if suffix == ".md" else "catalog" if has_md else "map"
            if item["action"] in {"semantic", "full", "extract"} and (
                suffix not in SUPPORTED_EXTENSIONS or item["bytes"] > 20_000_000
            ):
                item.update(state="skipped", reason="不支持解析或超过本次20MB上限")
        plan["request"]["rules"] = rules
        plan["actions"] = dict(Counter(i["action"] for i in plan["items"]))

    def policy(self, job_id, payload: IntakePolicy):
        with self.guard:
            plan = self.read(job_id)
            if plan["state"] != "ready" or not plan["scan_complete"]:
                raise ValueError("只有完整扫描且尚未执行的任务能修改处理方式")
            self._set_rules(plan, payload.rules)
            self._save(plan)
            return self.view(job_id)

    def run(self, job_id, confirmed=False, confirmed_vector=False):
        with self.guard:
            plan = self.read(job_id)
            if not confirmed or not plan["scan_complete"]:
                raise ValueError("必须先完成范围预览，再明确确认执行")
            if any(i["action"] == "semantic" for i in plan["items"]) and not confirmed_vector:
                raise ValueError("选择全文加向量时，必须确认可能调用云端 Embedding")
            if plan["state"] not in {"ready", "cancelled", "interrupted", "failed", "warning"}:
                raise ValueError("该任务不能执行或已完成")
            if (
                datetime.now(UTC) - datetime.fromisoformat(plan["created_at"])
            ).total_seconds() > 86400:
                raise ValueError("预览超过24小时，请重新预览")
            if self.thread and self.thread.is_alive():
                raise ValueError("已有任务运行")
            for item in plan["items"]:
                if item["state"] == "error":
                    item["state"] = "pending"
            plan.pop("error", None)
            plan["state"] = "backing_up"
            self._save(plan)
            self._launch(job_id, self._execute, plan)
        return self.view(job_id)

    def requires_vector(self, job_id):
        plan = self.read(job_id)
        return any(item["action"] == "semantic" for item in plan["items"])

    def cancel(self, job_id):
        if self.active != job_id:
            raise ValueError("任务当前未运行")
        self.stop.set()
        return {"requested": True, "note": "当前文件结束后停止；已完成的内容不会删除"}

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _backup(self, plan):
        directory = self.home / "recovery" / plan["id"]
        target = directory / "pkas.sqlite"
        if target.exists():
            # Keep the first recovery point on retries, never overwrite it.
            with closing(sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)) as check:
                if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("恢复点损坏，停止写入")
            return
        source = self.settings.database_path
        if shutil.disk_usage(self.home).free < source.stat().st_size * 2 + 100_000_000:
            raise ValueError("空间不足以保留恢复点，停止入库")
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / "pending.sqlite"
        with (
            closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as src,
            closing(sqlite3.connect(partial)) as dst,
        ):
            src.backup(dst)
            if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("恢复点校验失败")
        os.replace(partial, target)
        plan["recovery"] = str(target)
        plan["recovery_sha256"] = sha256_file(target)
        self._save(plan)

    def _retire_completed_backups(self, plan):
        """Rotate only our verified completed-batch recovery files, never other backups."""
        if plan["state"] != "completed" or not plan.get("recovery_sha256"):
            return
        current = Path(plan["recovery"])
        if sha256_file(current) != plan["recovery_sha256"]:
            plan["retention_warning"] = "恢复点校验变化，保留所有旧恢复点"
            return
        released = 0
        for manifest in self.home.glob("*.json"):
            old = self.read(manifest.stem)
            if old["id"] == plan["id"] or old["state"] != "completed":
                continue
            expected = self.home / "recovery" / old["id"] / "pkas.sqlite"
            if (
                old.get("recovery") != str(expected)
                or not old.get("recovery_sha256")
                or not expected.exists()
                or linked(expected)
            ):
                continue
            if sha256_file(expected) != old["recovery_sha256"]:
                continue
            size = expected.stat().st_size
            expected.unlink()
            released += size
            old["recovery_retired_by"] = plan["id"]
            self._save(old)
        plan["superseded_recovery_bytes_released"] = released

    def _checked(self, plan, item):
        path = Path(item["path"])
        roots = [
            Path(value).resolve(strict=False)
            for value in plan.get("allowed_roots", [plan["root"]])
        ]
        resolved = path.resolve(strict=True)
        if linked(path) or not any(root in resolved.parents for root in roots):
            raise IntakeItemError("路径边界或链接发生变化，请重新预览")
        stat = resolved.stat()
        if stat.st_size != item["bytes"] or stat.st_mtime_ns != item["mtime_ns"]:
            raise IntakeItemError("文件在预览后改变，请重新预览")
        return resolved

    def _document(self, plan, item):
        path = self._checked(plan, item)
        digest = sha256_file(path)
        repo = self.system.repository
        if item["action"] in {"semantic", "full"}:
            old = repo.source_by_hash(digest)
            if old:
                if old["status"] != "indexed":
                    raise IntakeItemError("已有同内容历史版本，需核查版本状态")
                return {"state": "duplicate", "source_id": old["id"]}
        parsed = parse_file(path, settings=self.settings, privacy="private")
        if not parsed.text.strip():
            raise ParseError("没有可提取文字")
        _, secrets = redact_secrets(parsed.text)
        if secrets:
            raise IntakeItemError("内容含疑似凭据，未入库")
        if sha256_file(path) != digest:
            raise IntakeItemError("读取期间原件发生变化，请重新预览")
        if item["action"] == "extract":
            lines = [line.strip() for line in parsed.text.splitlines() if line.strip()]
            step = max(1, len(lines) // 12)
            excerpt = "\n\n".join(line[:450] for line in lines[::step][:12])
            text = (
                f"# 本地摘录：{path.name}\n\n"
                f"> 自动抽取部分文字，不是AI总结，不代表全文或已验证事实。\n\n"
                f"来源：{path}\n\n版本 SHA256：{digest}\n\n{excerpt}"
            )
            result = IngestionService(self.settings, repo).import_text(
                text=text,
                title=f"本地摘录 {path.name}",
                original_uri=f"intake-extract://{hashlib.sha256(str(path).encode()).hexdigest()}/{digest}",
                source_type="local-extract",
                domain="work",
                privacy="private",
                metadata={
                    "original_path": str(path),
                    "original_hash": digest,
                    "derived_kind": "extractive_excerpt",
                    "review_status": "unreviewed",
                    "coverage": "partial",
                    "generated_at": plan["created_at"],
                },
            )
        else:
            chunks = chunk_document(parsed)
            if not chunks:
                raise ParseError("没有可索引文字")
            parsed.metadata.update(
                storage_mode="reference",
                original_hash=digest,
                requested_processing_level="L3" if item["action"] == "semantic" else "L2",
                content_category_id=item.get("content_category_id"),
                content_category_label=item.get("content_category_label"),
                classification_basis=item.get("classification_basis"),
                classification_revision=item.get("classification_revision"),
                source_summary_job=plan.get("request", {}).get("source_summary_job"),
            )
            previous = repo.source_by_uri(str(path))
            result = repo.add_document(
                original_uri=str(path),
                original_name=path.name,
                vault_path=str(path),
                source_type=path.suffix.lstrip("."),
                content_hash=digest,
                byte_size=item["bytes"],
                domain="work",
                privacy="private",
                parsed=parsed,
                chunks=chunks,
                source_created_at=datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            )
            if previous and previous["id"] != result["source_id"]:
                repo.set_source_status(previous["id"], "superseded")
        return {
            "state": "indexed",
            "source_id": result["source_id"],
            "storage": "generated_md" if item["action"] == "extract" else "reference",
            "vector": (
                "待本批向量同步"
                if item["action"] == "semantic"
                else "未执行；由独立索引任务处理"
            ),
        }

    def _execute(self, plan):
        # Prevent concurrent intake writers from another desktop/API instance.
        with WindowsFileLock(self.home / "writer.lock"):
            writable = any(
                i["state"] == "pending"
                and i["action"] in {"semantic", "full", "extract", "map"}
                for i in plan["items"]
            )
            if writable:
                self._backup(plan)
            plan["state"] = "running"
            self._save(plan)
            for item in plan["items"]:
                if self.stop.is_set():
                    plan["state"] = "cancelled"
                    self._save(plan)
                    return
                if item["state"] != "pending":
                    continue
                try:
                    self._checked(plan, item)
                    if item["action"] in {"catalog", "map"}:
                        item["state"] = "cataloged"
                    else:
                        item.update(self._document(plan, item))
                except Exception as exc:
                    item.update(
                        state="error",
                        reason=(
                            str(exc)
                            if isinstance(exc, IntakeItemError)
                            else f"{type(exc).__name__}：解析/读取失败，请检查格式与可读权限"
                        ),
                    )
                self._save(plan)
            if self.stop.is_set():
                plan["state"] = "cancelled"
                self._save(plan)
                return
            maps = [i for i in plan["items"] if i["action"] == "map" and i["state"] == "cataloged"]
            if maps and not plan.get("map_source_id"):
                text = (
                    "# 资料目录清单\n\n> 仅根据文件路径和大小生成；"
                    "未读取正文，不是项目内容总结。\n\n"
                    f"范围：{plan['root']}\n\n"
                    + "\n".join(f"- {i['relative']}（{i['bytes']} 字节）" for i in maps)
                )
                result = IngestionService(self.settings, self.system.repository).import_text(
                    text=text,
                    title="资料目录清单",
                    original_uri=f"intake-map://{plan['id']}",
                    source_type="directory-map",
                    domain="work",
                    privacy="private",
                    metadata={
                        "derived_kind": "directory_listing",
                        "original_path": plan["root"],
                        "generated_at": plan["created_at"],
                        "coverage": "filenames_only",
                    },
                )
                plan["map_source_id"] = result["source_id"]
            semantic_items = [
                item
                for item in plan["items"]
                if item["action"] == "semantic"
                and item["state"] in {"indexed", "duplicate"}
            ]
            vector_ok = True
            if semantic_items and not self.stop.is_set():
                plan["stage"] = "vectorizing"
                plan["message"] = "正文切块已入库，正在同步向量"
                self._save(plan)
                vector_result = self.system.outbox.process(limit=5000)
                coverage = self.system.rag.vector_index.coverage()
                vector_ok = (
                    vector_result.get("status") == "completed"
                    and not vector_result.get("deferred")
                    and int(coverage.get("pending", 0)) == 0
                )
                plan["vector_result"] = vector_result
                plan["vector_coverage"] = coverage
                for item in semantic_items:
                    item["vector"] = (
                        "向量同步已完成"
                        if vector_ok
                        else "正文已入库；向量未完成，可稍后重试"
                    )
            plan["state"] = (
                "warning"
                if (
                    any(i["state"] in {"error", "skipped"} for i in plan["items"])
                    or not vector_ok
                )
                else "completed"
            )
            plan["stage"] = "done"
            self._save(plan)
            try:
                self._retire_completed_backups(plan)
            except (OSError, ValueError):
                plan["retention_warning"] = "旧恢复点整理未完成，保留现有文件"
            self._save(plan)
