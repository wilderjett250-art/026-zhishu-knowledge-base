from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.capability_registry import CodexClientAdapter


def test_section_cache_and_whitelist(knowledge_system, monkeypatch):
    service = knowledge_system.readiness
    calls = []

    def runtime():
        calls.append(1)
        return {"id": "runtime", "status": "paused"}

    monkeypatch.setattr(service, "_runtime_gate", runtime)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.section("runtime"), range(4)))
    assert len(calls) == 1
    assert all(item["checked_at"] == results[0]["checked_at"] for item in results)
    assert sum(not item["cached"] for item in results) == 1
    with pytest.raises(ValueError):
        service.section("__dict__")


def test_quick_skill_inventory_does_not_hash_resources(tmp_path, monkeypatch):
    skill = tmp_path / "demo"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")

    def forbidden(*args):
        raise AssertionError("Overview must not perform a content audit")

    monkeypatch.setattr("pkas.capability_registry._directory_fingerprint", forbidden)
    result = CodexClientAdapter(audit_resources=False)._skills(tmp_path)
    assert result[0]["validation"] == "not_checked"


def test_section_endpoint_isolated_from_full_report(test_settings, monkeypatch):
    with TestClient(create_app(test_settings)) as client:
        service = client.app.state.system.readiness

        def forbidden():
            raise AssertionError("Must not run all checks")

        monkeypatch.setattr(service, "report", forbidden)
        response = client.get("/api/core/readiness/runtime")
        assert response.status_code == 200
        assert response.json()["data"]["checked_at"]
        assert client.get("/api/core/readiness/invalid").status_code == 404


def test_section_snapshot_returns_without_waiting_for_slow_database_check(
    knowledge_system, monkeypatch
):
    service = knowledge_system.readiness
    started = Event()
    release = Event()

    def delayed_database():
        started.set()
        assert release.wait(timeout=2)
        return {"id": "database", "status": "passed", "metrics": {}}

    monkeypatch.setattr(service, "_database_gate", delayed_database)
    began = monotonic()
    pending = service.section_snapshot("database")
    assert monotonic() - began < 0.2
    assert pending["snapshot_state"] == "refreshing"
    assert started.wait(timeout=1)
    try:
        second_began = monotonic()
        still_pending = service.section_snapshot("database")
        assert monotonic() - second_began < 0.2
        assert still_pending["snapshot_state"] == "refreshing"
    finally:
        release.set()

    deadline = monotonic() + 2
    current = pending
    while monotonic() < deadline:
        current = service.section_snapshot("database")
        if current["snapshot_state"] == "ready":
            break
        sleep(0.01)
    assert current["snapshot_state"] == "ready"
    assert current["status"] == "passed"


def test_stale_cached_snapshot_keeps_result_visible_while_refreshing(
    knowledge_system, monkeypatch
):
    service = knowledge_system.readiness
    started = Event()
    release = Event()
    service._section_cache["database"] = (
        monotonic() - 31,
        {
            "id": "database",
            "status": "passed",
            "metrics": {"chunks": 1},
            "checked_at": "cached",
            "cached": False,
        },
    )

    def delayed_database():
        started.set()
        assert release.wait(timeout=2)
        return {"id": "database", "status": "passed", "metrics": {"chunks": 2}}

    monkeypatch.setattr(service, "_database_gate", delayed_database)
    try:
        snapshot = service.section_snapshot("database")
        assert snapshot["snapshot_state"] == "ready"
        assert snapshot["refreshing"] is True
        assert snapshot["cached"] is True
        assert started.wait(timeout=1)
    finally:
        release.set()
