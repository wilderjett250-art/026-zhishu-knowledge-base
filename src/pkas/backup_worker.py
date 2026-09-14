from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from pkas.backup_manager import BackupManager
from pkas.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a verified PKAS recovery bundle.")
    parser.add_argument("--label", default="scheduled")
    parser.add_argument("--retention", type=int, default=3)
    args = parser.parse_args()
    settings = get_settings()
    manager = BackupManager(settings)
    try:
        report = manager.create_bundle(label=args.label, retention=args.retention)
    except Exception as exc:
        report = {"status": "failed", "error_code": type(exc).__name__}
    report_path = settings.data_root / "runs" / "backups" / (
        "backup-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + ".json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report["report_path"] = str(report_path)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["status"] in {"completed", "already_running"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
