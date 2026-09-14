from __future__ import annotations

import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote

from pkas.capability_registry import (
    CapabilityRegistry,
    ClaudeDesktopClientAdapter,
    CodexClientAdapter,
    CursorClientAdapter,
)
from pkas.db import Database
from pkas.profile_service import CapabilityProfileService
from pkas.rag_observability import RagObservabilityService


class CoreReadinessService:
    schema_version = "pkas.core-readiness.v1"

    def __init__(
        self,
        database: Database,
        rag: RagObservabilityService,
        profiles: CapabilityProfileService,
        integration_home: Path,
        reports_root: Path,
        data_root: Path,
    ) -> None:
        self.database = database
        self.rag = rag
        self.profiles = profiles
        self.integration_home = integration_home
        self.reports_root = reports_root
        self.data_root = data_root
        self._section_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._section_locks = {key: Lock() for key in self.section_names}

    section_names = ("database", "retrieval", "capability", "evaluation", "runtime", "quality")

    def section(self, name: str) -> dict[str, Any]:
        if name not in self.section_names:
            raise ValueError("Unknown readiness section")
        with self._section_locks[name]:
            cached = self._section_cache.get(name)
            if cached and time.monotonic() - cached[0] < 30:
                return {**cached[1], "cached": True}
            gate = getattr(self, f"_{name}_gate")()
            result = {**gate, "checked_at": datetime.now(UTC).isoformat(), "cached": False}
            self._section_cache[name] = (time.monotonic(), result)
            return result

    def report(self) -> dict[str, Any]:
        gates = [
            self._database_gate(),
            self._retrieval_gate(),
            self._capability_gate(),
            self._evaluation_gate(),
            self._runtime_gate(),
            self._quality_gate(),
        ]
        factors = {
            "passed": 1.0,
            "partial": 0.55,
            "paused": 0.8,
            "blocked": 0.0,
            "unknown": 0.25,
        }
        score = round(sum(factors[item["status"]] * item["weight"] for item in gates))
        blocking = [item["id"] for item in gates if item["status"] == "blocked"]
        local_trial = not blocking and score >= 70
        return {
            "schema_version": self.schema_version,
            "score": score,
            "status": "ready_for_local_trial" if local_trial else "not_ready",
            "gates": gates,
            "blocking_gates": blocking,
            "claims": {
                "local_trial": local_trial,
                "production_complete": False,
                "cross_platform_complete": False,
            },
            "next_required": [
                item["next_action"]
                for item in gates
                if item["status"] in {"partial", "blocked", "unknown"}
            ],
        }

    def _database_gate(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            schema = int(
                connection.execute(
                    "SELECT value FROM app_meta WHERE key='schema_version'"
                ).fetchone()[0]
            )
            chunks = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            chunk_fts = int(
                connection.execute("SELECT COUNT(*) FROM chunks_fts_docsize").fetchone()[0]
            )
            messages = int(
                connection.execute("SELECT COUNT(*) FROM customer_messages").fetchone()[0]
            )
            message_fts = int(
                connection.execute("SELECT COUNT(*) FROM customer_messages_fts_docsize").fetchone()[
                    0
                ]
            )
            orphan_links = int(
                connection.execute(
                    """SELECT COUNT(*) FROM chunk_blocks cb
                    LEFT JOIN chunks c ON c.id=cb.chunk_id
                    LEFT JOIN blocks b ON b.id=cb.block_id
                    WHERE c.id IS NULL OR b.id IS NULL"""
                ).fetchone()[0]
            )
        passed = (
            schema >= 18 and chunks == chunk_fts and messages == message_fts and orphan_links == 0
        )
        return {
            "id": "database",
            "name": "事实库与全文索引",
            "weight": 22,
            "status": "passed" if passed else "blocked",
            "metrics": {
                "schema": schema,
                "chunks": chunks,
                "chunk_fts": chunk_fts,
                "customer_messages": messages,
                "customer_message_fts": message_fts,
                "orphan_links": orphan_links,
            },
            "evidence": "当前数据库行数与关系只读对账",
            "next_action": "修复全文索引或父子关系后重新验收",
        }

    def _retrieval_gate(self) -> dict[str, Any]:
        vector = self.rag.vector_index
        scope = vector.coverage()
        eligible = int(scope["eligible"])
        indexed = int(scope["indexed"])
        coverage = 1.0 if eligible == 0 else indexed / eligible
        vector_state = "disabled"
        if vector.enabled:
            url = (
                vector.settings.qdrant_url.rstrip("/")
                + "/collections/"
                + quote(vector.settings.qdrant_collection, safe="")
            )
            headers = {}
            if vector.settings.qdrant_api_key:
                headers["api-key"] = vector.settings.qdrant_api_key.get_secret_value()
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=1
                ) as response:
                    payload = json.loads(response.read(1_000_000))
                vector_state = "ready" if isinstance(payload.get("result"), dict) else "warning"
            except (OSError, ValueError, TypeError):
                vector_state = "offline"
        if coverage >= 0.999 and vector_state == "ready":
            state = "passed"
        elif coverage >= 0.999 and vector_state in {"offline", "warning", "not_checked"}:
            state = "paused"
        else:
            state = "partial"
        return {
            "id": "retrieval",
            "name": "混合RAG与向量覆盖",
            "weight": 22,
            "status": state,
            "metrics": {
                "eligible_vectors": eligible,
                "indexed_vectors": indexed,
                "coverage": coverage,
                "vector_runtime": vector_state,
            },
            "evidence": "本地索引台账覆盖率与限时服务探测；不代表真实查询验收",
            "next_action": "启动向量侧车后执行真实混合查询验收",
        }

    def _capability_gate(self) -> dict[str, Any]:
        overview = CapabilityRegistry(
            self.integration_home,
            adapters=[
                CodexClientAdapter(audit_resources=False),
                ClaudeDesktopClientAdapter(),
                CursorClientAdapter(),
            ],
        ).overview(
            knowledge_stats={"counts": {"profiles": len(self.profiles.list_profiles())}},
            rag_status={
                "qdrant": {"status": "not_checked", "points": 0},
                "mcp_enabled": False,
            },
        )
        skills = len(overview["skills"])
        mcp_servers = len(overview["mcp_servers"])
        profiles = int(overview["summary"]["profiles"])
        return {
            "id": "capabilities",
            "name": "Skill、MCP与Profile管理",
            "weight": 18,
            "status": "passed" if skills > 0 and profiles > 0 else "partial",
            "metrics": {"skills": skills, "mcp_servers": mcp_servers, "profiles": profiles},
            "evidence": "脱敏能力目录与Profile数据库",
            "next_action": "完成MCP显式连接测试和Profile运行时门禁",
        }

    def _evaluation_gate(self) -> dict[str, Any]:
        silver = self.rag.latest_silver_gate() or {}
        gold = self.rag.review_progress()
        silver_passed = silver.get("status") == "passed"
        eligible = int(gold.get("eligible", 0))
        total = int(gold.get("total", 0))
        return {
            "id": "evaluation",
            "name": "检索测评与人工金标",
            "weight": 18,
            "status": "passed" if silver_passed and total > 0 and eligible >= total else "partial",
            "metrics": {
                "silver_passed": silver_passed,
                "silver_hit_rate": silver.get("hit_rate"),
                "gold_eligible": eligible,
                "gold_total": total,
            },
            "evidence": "最新银标报告与人工复核进度",
            "next_action": f"完成人工金标复核（当前 {eligible}/{total}）",
        }

    def _runtime_gate(self) -> dict[str, Any]:
        policy_path = self.data_root / "config" / "windows_autostart_policy.json"
        enabled = False
        if policy_path.is_file():
            try:
                enabled = (
                    json.loads(policy_path.read_text(encoding="utf-8"))["autostart_enabled"] is True
                )
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                enabled = False
        return {
            "id": "runtime",
            "name": "本机运行策略",
            "weight": 8,
            "status": "passed" if enabled else "paused",
            "metrics": {"autostart_enabled": enabled, "visible_terminal_required": False},
            "evidence": "本地fail-closed运行策略",
            "next_action": "本地试用阶段使用无可见终端的按需启动器",
        }

    def _quality_gate(self) -> dict[str, Any]:
        reports = sorted(
            self.reports_root.glob("*pytest.xml"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not reports:
            return {
                "id": "quality",
                "name": "自动化回归",
                "weight": 12,
                "status": "unknown",
                "metrics": {"tests": 0, "failures": None, "errors": None},
                "evidence": "未找到JUnit测试报告",
                "next_action": "运行项目全量测试并保存JUnit报告",
            }
        try:
            suite = ET.parse(reports[0]).getroot().find("testsuite")
            metrics = {
                "tests": int(suite.attrib.get("tests", 0)) if suite is not None else 0,
                "failures": int(suite.attrib.get("failures", 0)) if suite is not None else 0,
                "errors": int(suite.attrib.get("errors", 0)) if suite is not None else 0,
                "skipped": int(suite.attrib.get("skipped", 0)) if suite is not None else 0,
            }
        except (ET.ParseError, OSError, ValueError):
            metrics = {"tests": 0, "failures": 1, "errors": 1, "skipped": 0}
        passed = metrics["tests"] > 0 and metrics["failures"] == 0 and metrics["errors"] == 0
        return {
            "id": "quality",
            "name": "自动化回归",
            "weight": 12,
            "status": "passed" if passed else "blocked",
            "metrics": metrics,
            "evidence": "最新JUnit全量测试报告",
            "next_action": "修复失败测试并重新生成报告",
        }
