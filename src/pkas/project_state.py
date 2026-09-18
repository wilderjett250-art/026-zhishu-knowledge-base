import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pkas.ingest import is_sensitive_path
from pkas.repository import Repository


class ProjectInspectionError(ValueError):
    pass


class ProjectInspector:
    def __init__(self, repository: Repository | None = None) -> None:
        self.repository = repository or Repository()

    def inspect(self, workspace_path: str) -> dict[str, Any]:
        workspace = Path(workspace_path).expanduser().resolve(strict=False)
        if not workspace.is_dir():
            raise ProjectInspectionError("工作区不存在或不是目录。")
        authorized_root = self.repository.authorized_sync_root(str(workspace))
        if not authorized_root:
            raise ProjectInspectionError("工作区不在已经授权的知识库资料范围内。")

        git_root_result = self._git(workspace, "rev-parse", "--show-toplevel")
        if git_root_result["returncode"] != 0:
            entries = sorted(item.name for item in workspace.iterdir())[:100]
            return {
                "status": "warning",
                "summary": "已确认授权工作区，但该目录不是 Git 仓库。",
                "workspace_path": str(workspace),
                "authorized_root_id": authorized_root["id"],
                "is_git_repository": False,
                "top_level_entries": entries,
                "observed_at": datetime.now(UTC).isoformat(),
            }

        git_root = Path(git_root_result["stdout"]).resolve(strict=False)
        if not self.repository.authorized_sync_root(str(git_root)):
            raise ProjectInspectionError("Git 根目录超出了已经授权的知识库资料范围。")
        branch = self._git(git_root, "branch", "--show-current")["stdout"]
        head_commit = self._git(git_root, "rev-parse", "HEAD")["stdout"]
        head_summary = self._git(git_root, "log", "-1", "--format=%cI | %s")["stdout"]
        status_result = self._git(
            git_root,
            "status",
            "--short",
            "--untracked-files=normal",
        )
        status_lines = [
            line.rstrip() for line in status_result["stdout"].splitlines() if line.strip()
        ]
        truncated = len(status_lines) > 200
        changed_files: list[dict[str, str]] = []
        sensitive_omitted = 0
        for line in status_lines[:200]:
            if len(line) < 4:
                continue
            relative_path = line[3:].strip()
            if is_sensitive_path(Path(relative_path)):
                sensitive_omitted += 1
                continue
            changed_files.append({"status": line[:2].strip(), "path": relative_path})
        return {
            "status": "success",
            "summary": (
                f"已读取 Git 开发状态：分支 {branch or '(detached)'}，"
                f"{len(status_lines)} 个工作区变化。"
            ),
            "workspace_path": str(workspace),
            "project_root": str(git_root),
            "authorized_root_id": authorized_root["id"],
            "is_git_repository": True,
            "branch": branch or None,
            "head_commit": head_commit or None,
            "head_summary": head_summary or None,
            "dirty": bool(status_lines),
            "changed_file_count": len(status_lines),
            "changed_files": changed_files,
            "sensitive_changed_files_omitted": sensitive_omitted,
            "changed_files_truncated": truncated,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _git(workspace: Path, *args: str) -> dict[str, Any]:
        try:
            process = subprocess.run(
                ["git", "-C", str(workspace), *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"returncode": 1, "stdout": "", "error_type": type(exc).__name__}
        return {
            "returncode": process.returncode,
            "stdout": process.stdout.rstrip(),
        }
