"""Own only children created by this API; never kill a process by an untrusted PID file."""

import json
import os
import subprocess
import threading
import time
import urllib.request

from pkas.config import Settings
from pkas.local_lock import WindowsFileLock


class RuntimeManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.children: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()
        self.errors: dict[str, str] = {}
        self.stopping: set[str] = set()

    def _qdrant_ready(self):
        try:
            with urllib.request.urlopen("http://127.0.0.1:6333/", timeout=0.4) as response:
                info = json.loads(response.read(4096))
            return info.get("title") == "qdrant - vector search engine"
        except (OSError, ValueError):
            return False

    def status(self):
        ready = self._qdrant_ready()
        services = [
            {
                "id": "api",
                "name": "管理后台",
                "status": "running",
                "owned": True,
                "controllable": False,
                "detail": "当前提供此页面的后台",
            }
        ]
        for name, label in (("qdrant", "向量服务"), ("indexer", "索引后台")):
            process = self.children.get(name)
            running = process is not None and process.poll() is None
            services.append(
                {
                    "id": name,
                    "name": label,
                    "owned": running,
                    "status": "stopping"
                    if name in self.stopping and running
                    else ("running" if name == "indexer" or ready else "starting")
                    if running
                    else "external"
                    if name == "qdrant" and ready
                    else "stopped"
                    if name in self.stopping
                    else "failed"
                    if name in self.errors or process is not None
                    else "not_managed",
                    "controllable": name != "qdrant" or not ready or running,
                    "detail": self.errors.get(
                        name,
                        "由统一后台启动；进程存活不等于任务成功"
                        if running
                        else "未托管不等于不存在其他进程；不接管外部进程",
                    ),
                }
            )
        return {
            "services": services,
            "autostart_changed": False,
            "sync": "手动触发；不随桌面启动",
            "agent": "不随桌面启动",
            "weflow": "仅手动同步时使用，不自动启动",
        }

    def ensure_qdrant_ready(self, timeout_seconds: float = 15.0):
        """Start the owned local sidecar when needed and wait for real HTTP readiness."""
        if self._qdrant_ready():
            return self.status()
        self.control("qdrant", "start")
        deadline = time.monotonic() + max(1.0, min(timeout_seconds, 30.0))
        while time.monotonic() < deadline:
            if self._qdrant_ready():
                return self.status()
            process = self.children.get("qdrant")
            if process is None or process.poll() is not None:
                self.errors["qdrant"] = "向量服务启动后提前退出"
                raise ValueError(self.errors["qdrant"])
            time.sleep(0.1)
        self.errors["qdrant"] = "向量服务未在限定时间内就绪"
        raise ValueError(self.errors["qdrant"])

    def control(self, name: str, action: str, confirm_cloud: bool = False):
        if name not in {"qdrant", "indexer"} or action not in {"start", "stop"}:
            raise ValueError("不支持的运行操作")
        with self.lock:
            process = self.children.get(name)
            running = process is not None and process.poll() is None
            if action == "stop":
                if not running:
                    raise ValueError("不能停止非本管理器启动的进程")
                if name == "indexer":
                    self.stopping.add(name)
                    (self.settings.data_root / "runtime" / "indexer.stop").touch()
                else:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    return {**self.status(), "message": "已请求停止，等待当前索引批次结束"}
                del self.children[name]
                self.errors.pop(name, None)
                return self.status()
            if running:
                return self.status()
            self.stopping.discard(name)
            if name == "qdrant" and self._qdrant_ready():
                raise ValueError("向量服务由其他入口启动，本次不重复启动或接管")
            if name == "indexer" and not confirm_cloud:
                raise ValueError("索引可能调用Embedding API，请明确确认后启动")
            root = self.settings.project_root
            run = self.settings.data_root / "runtime"
            run.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["PKAS_DATA_ROOT"] = str(self.settings.data_root)
            env["PKAS_PROJECT_ROOT"] = str(root)
            if name == "qdrant":
                executable = root / "runtime" / "qdrant" / "qdrant.exe"
                arguments = [str(executable)]
                env.update(
                    {
                        "QDRANT__STORAGE__STORAGE_PATH": str(
                            self.settings.data_root / "qdrant" / "storage"
                        ),
                        "QDRANT__STORAGE__SNAPSHOTS_PATH": str(
                            self.settings.data_root / "qdrant" / "snapshots"
                        ),
                        "QDRANT__SERVICE__HOST": "127.0.0.1",
                        "QDRANT__SERVICE__HTTP_PORT": "6333",
                        "QDRANT__SERVICE__GRPC_PORT": "6334",
                        "QDRANT__TELEMETRY_DISABLED": "true",
                    }
                )
            else:
                with WindowsFileLock(self.settings.data_root / "runtime" / "pkas-core.lock"):
                    pass
                executable = root / ".venv" / "Scripts" / "pythonw.exe"
                stop_file = run / "indexer.stop"
                stop_file.unlink(missing_ok=True)
                arguments = [
                    str(executable),
                    "-m",
                    "pkas.core_worker",
                    "--outbox-only",
                    "--interval-seconds",
                    "300",
                    "--stop-file",
                    str(stop_file),
                ]
            if not executable.is_file():
                raise ValueError("本地运行依赖缺失，未启动")
            try:
                with (run / f"{name}.log").open("ab") as log:
                    self.children[name] = subprocess.Popen(
                        arguments,
                        cwd=root,
                        env=env,
                        stdout=log,
                        stderr=log,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                self.errors.pop(name, None)
            except OSError:
                self.errors[name] = "进程启动失败，请检查本地依赖"
                raise ValueError(self.errors[name]) from None
            return self.status()

    def close(self):
        for name in list(self.children):
            if self.children[name].poll() is None:
                self.control(name, "stop")
