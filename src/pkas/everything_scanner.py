"""Official portable file-list provider; no service/autostart or persistent index."""

import csv
import hashlib
import subprocess
import time
from pathlib import Path

EXPECTED_HASH = "f191f756996a14a11e5445fa7103d302efd510cf2fbf920e6c0c8ed51d512e36"


class EverythingScanError(ValueError):
    pass


def executable(project_root: Path) -> Path:
    return project_root / "tools" / "everything" / "Everything.exe"


def status(project_root: Path) -> dict:
    path = executable(project_root)
    valid = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == EXPECTED_HASH
    return {
        "available": valid,
        "version": "1.4.1.1032",
        "edition": "free-standard-portable",
        "mode": "scoped_file_list",
        "license": "tools/everything/License.txt",
        "note": "指定目录清单，不启动窗口、服务或全盘索引；不保证MFT索引速度。",
    }


def parse_file_list(path: Path, root: Path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or "Filename" not in reader.fieldnames:
            raise EverythingScanError("Everything 文件清单格式不正确，未接入")
        for row in reader:
            raw = Path(row["Filename"])
            if raw == root:
                continue
            if not raw.is_absolute() or root not in raw.resolve().parents:
                raise EverythingScanError("Everything 返回范围外路径，已停止")
            if int(row.get("Attributes") or "0") & (0x10 | 0x400):
                continue
            yield raw


def scan(project_root: Path, root: Path, output: Path, excluded: set[str], stop):
    if not status(project_root)["available"]:
        raise EverythingScanError("Everything 缺失或校验失败；请修复组件或手动选择普通扫描")
    if any(c in str(root) for c in ';"\r\n'):
        raise EverythingScanError("目录含扫描参数保留字符，不能使用 Everything")
    output.parent.mkdir(parents=True, exist_ok=True)
    args = [
        str(executable(project_root)),
        "-create-file-list",
        str(output),
        str(root),
        "-create-file-list-exclude-folders",
        ";".join(sorted(excluded)),
    ]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    start = time.monotonic()
    try:
        while proc.poll() is None:
            if stop.wait(0.1):
                raise EverythingScanError("用户已取消 Everything 扫描")
            if time.monotonic() - start > 120:
                raise EverythingScanError("Everything 扫描超过120秒，请缩小范围")
            if output.exists() and output.stat().st_size > 64_000_000:
                raise EverythingScanError("Everything 文件清单过大，请缩小范围")
        if proc.returncode != 0 or not output.is_file():
            raise EverythingScanError("Everything 未成功生成清单，请检查目录权限或组件状态")
        yield from parse_file_list(output, root)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        output.unlink(missing_ok=True)
