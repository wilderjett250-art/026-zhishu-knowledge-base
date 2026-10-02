import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts/check_release_versions.ps1"


def run_gate(root: Path) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell 5.1 required")
    return subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-File", str(GATE),
         "-ProjectRoot", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=20, env=os.environ.copy(),
    )


@pytest.fixture
def release_root(tmp_path: Path) -> Path:
    root = tmp_path / "Synthetic release with spaces"
    contents = {
        "pyproject.toml": '[project]\nname = "pkas"\nversion = "1.2.3"\n',
        "src/pkas/__init__.py": '__version__ = "1.2.3"\n',
        "src/pkas/config.py": 'class Settings:\n    app_version: str = "1.2.3"\n',
        "uv.lock": '[[package]]\nname = "pkas"\nversion = "1.2.3"\n',
        "desktop/src-tauri/Cargo.toml": '[package]\nname = "zhishu-desktop"\n'
        'version = "1.2.3"\n',
        "desktop/src-tauri/Cargo.lock": '[[package]]\nname = "zhishu-desktop"\n'
        'version = "1.2.3"\n',
        "desktop/src-tauri/tauri.conf.json": json.dumps({"version": "1.2.3"}),
        "web/package.json": json.dumps({"version": "1.2.3"}),
        "web/package-lock.json": json.dumps({
            "version": "1.2.3", "packages": {"": {"version": "1.2.3"}},
        }),
    }
    for relative, text in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_actual_project_release_metadata_is_consistent():
    result = run_gate(ROOT)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["checked_fields"] == 10


def test_synthetic_release_gate_is_read_only(release_root: Path):
    before = {str(p.relative_to(release_root)): p.read_bytes()
              for p in release_root.rglob("*") if p.is_file()}
    result = run_gate(release_root)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["version"] == "1.2.3"
    after = {str(p.relative_to(release_root)): p.read_bytes()
             for p in release_root.rglob("*") if p.is_file()}
    assert before == after


@pytest.mark.parametrize("relative", [
    "src/pkas/__init__.py", "src/pkas/config.py", "uv.lock",
    "desktop/src-tauri/Cargo.toml", "desktop/src-tauri/Cargo.lock",
    "desktop/src-tauri/tauri.conf.json", "web/package.json", "web/package-lock.json",
])
def test_mismatched_component_stops_release(release_root: Path, relative: str):
    path = release_root / relative
    path.write_text(path.read_text(encoding="utf-8").replace("1.2.3", "1.2.2"), encoding="utf-8")
    result = run_gate(release_root)
    assert result.returncode != 0
    assert relative in result.stderr


def test_web_lock_root_package_cannot_keep_an_old_version(release_root: Path):
    path = release_root / "web/package-lock.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["packages"][""]["version"] = "1.2.2"
    path.write_text(json.dumps(value), encoding="utf-8")
    result = run_gate(release_root)
    assert result.returncode != 0
    assert "web/package-lock.json:root-package" in result.stderr


def test_missing_metadata_stops_release(release_root: Path):
    (release_root / "src/pkas/__init__.py").unlink()
    result = run_gate(release_root)
    assert result.returncode != 0
    assert "Release metadata is missing" in result.stderr


@pytest.mark.parametrize("bad", [None, [], {"version": ["1.2.3"]}])
def test_invalid_json_metadata_is_not_coerced_to_a_version(release_root: Path, bad):
    (release_root / "web/package.json").write_text(json.dumps(bad), encoding="utf-8")
    result = run_gate(release_root)
    assert result.returncode != 0
    assert "web/package.json" in result.stderr


def test_packaging_checks_versions_before_staging_or_downloading():
    source = (ROOT / "scripts/prepare_tauri_resources.ps1").read_text(encoding="utf-8")
    assert source.index("check_release_versions.ps1") < source.index("New-Item")
    assert source.index("check_release_versions.ps1") < source.index("$uvArchive = Get-Archive")
