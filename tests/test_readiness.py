from pathlib import Path

from pkas.capability_registry import CapabilityRegistry
from pkas.readiness import CoreReadinessService


def test_core_readiness_reports_evidence_boundaries(knowledge_system, monkeypatch) -> None:
    knowledge_system.database.initialize()
    monkeypatch.setattr(
        knowledge_system.rag,
        "status",
        lambda: {
            "coverage_scope": {"vector_eligible_chunks": 0, "vector_indexed_chunks": 0},
            "qdrant": {"status": "offline"},
        },
    )
    monkeypatch.setattr(
        knowledge_system.rag,
        "latest_silver_gate",
        lambda: {"status": "passed", "hit_rate": 0.98},
    )
    monkeypatch.setattr(
        knowledge_system.rag,
        "review_progress",
        lambda: {"eligible": 0, "total": 150},
    )
    monkeypatch.setattr(
        CapabilityRegistry,
        "overview",
        lambda *_args, **_kwargs: {
            "skills": [{"id": "skill"}],
            "mcp_servers": [],
            "summary": {"profiles": 0},
        },
    )
    reports = knowledge_system.settings.project_root / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "full-pytest.xml").write_text(
        '<testsuites><testsuite tests="10" failures="0" errors="0" skipped="0" /></testsuites>',
        encoding="utf-8",
    )
    service = CoreReadinessService(
        knowledge_system.database,
        knowledge_system.rag,
        knowledge_system.profiles,
        Path("integration"),
        reports,
        knowledge_system.settings.data_root,
    )
    report = service.report()
    assert report["claims"]["production_complete"] is False
    assert report["claims"]["cross_platform_complete"] is False
    assert {item["id"] for item in report["gates"]} == {
        "database",
        "retrieval",
        "capabilities",
        "evaluation",
        "runtime",
        "quality",
    }
    evaluation = next(item for item in report["gates"] if item["id"] == "evaluation")
    assert evaluation["status"] == "partial"
    assert "0/150" in evaluation["next_action"]
