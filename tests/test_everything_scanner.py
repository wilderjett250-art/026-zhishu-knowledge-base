import csv
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient
from test_intake import execute, preview, wait

from pkas import everything_scanner as scanner
from pkas.api import create_app
from pkas.intake import DEFAULT_RULES, IntakePolicy, IntakeService


def write_efu(path, paths):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Filename", "Size", "Date Modified", "Attributes"])
        for p in paths:
            writer.writerow([str(p), 10, 0, 32])


def test_efu_unicode_spaces_and_scope(tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    paths = [root / "a,b.md", root / "中文 空格.txt"]
    efu = tmp_path / "test.efu"
    write_efu(efu, paths)
    assert list(scanner.parse_file_list(efu, root)) == paths
    write_efu(efu, [tmp_path / "outside.md"])
    with pytest.raises(scanner.EverythingScanError):
        list(scanner.parse_file_list(efu, root))


def test_component_does_not_execute(tmp_path):
    assert scanner.status(tmp_path)["available"] is False
    path = scanner.executable(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"untrusted")
    assert scanner.status(tmp_path)["available"] is False


def test_native_process_contract_with_fake_process(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    efu = tmp_path / "test.efu"
    monkeypatch.setattr(scanner, "status", lambda _: {"available": True})
    captured = []

    class FakeProcess:
        returncode = 0

        def __init__(self, args, **kwargs):
            captured.append((args, kwargs))
            write_efu(efu, [root / "a.md"])

        def poll(self):
            return 0

    monkeypatch.setattr(scanner.subprocess, "Popen", FakeProcess)
    assert list(scanner.scan(tmp_path, root, efu, {"node_modules"}, threading.Event())) == [
        root / "a.md"
    ]
    args, opts = captured[0]
    assert "-create-file-list" in args
    assert "-install-service" not in args
    assert opts["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert not efu.exists()


def test_cancel_kills_only_own_fake_process(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "status", lambda _: {"available": True})
    terminated = []

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            terminated.append(True)

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(scanner.subprocess, "Popen", lambda *a, **k: FakeProcess())
    stop = threading.Event()
    stop.set()
    with pytest.raises(scanner.EverythingScanError):
        list(scanner.scan(tmp_path, tmp_path, tmp_path / "x.efu", set(), stop))
    assert terminated == [True]


def test_no_silent_fallback(knowledge_system, source_root, monkeypatch):
    service = IntakeService(knowledge_system)

    def fail(*args):
        raise scanner.EverythingScanError("synthetic failure")

    monkeypatch.setattr("pkas.intake.everything_scan", fail)
    plan = preview(service, source_root, scanner="everything")
    assert plan["state"] == "failed"
    assert not plan["scan_complete"]
    assert plan["scanner"] == "everything"


def test_discovery_then_policy_without_rescan(knowledge_system, source_root, monkeypatch):
    a = source_root / "a.md"
    a.write_text("discovery fixture")
    b = source_root / "b.py"
    b.write_text("print(1)")
    calls = []

    def discover(*args):
        calls.append(True)
        yield a
        yield b

    monkeypatch.setattr("pkas.intake.everything_scan", discover)
    service = IntakeService(knowledge_system)
    plan = preview(service, source_root, scanner="everything")
    assert service.view(plan["id"])["categories"]["code"]["count"] == 1
    with knowledge_system.database.connect() as c:
        assert c.execute("select count(*) from sources").fetchone()[0] == 0
    result = service.policy(plan["id"], IntakePolicy(rules=dict.fromkeys(DEFAULT_RULES, "catalog")))
    assert result["actions"] == {"catalog": 2}
    assert calls == [True]
    assert execute(service, plan)["state"] == "completed"
    with pytest.raises(ValueError):
        service.policy(plan["id"], IntakePolicy(rules=DEFAULT_RULES))


def test_policy_endpoint(test_settings, source_root):
    (source_root / "a.md").write_text("policy fixture")
    with TestClient(create_app(test_settings)) as client:
        assert client.get("/api/foundation/everything").json()["data"]["available"] is False
        plan = client.post(
            "/api/foundation/intake/preview", json={"path": str(source_root)}
        ).json()["data"]
        wait(client.app.state.intake)
        response = client.post(
            f"/api/foundation/intake-policy/{plan['id']}",
            json={"rules": dict.fromkeys(DEFAULT_RULES, "exclude")},
        )
        assert response.status_code == 200
        assert response.json()["data"]["actions"] == {"exclude": 1}


def test_root_parameter_injection_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "status", lambda _: {"available": True})
    with pytest.raises(scanner.EverythingScanError):
        list(scanner.scan(tmp_path, tmp_path / "a;b", tmp_path / "x.efu", set(), threading.Event()))
