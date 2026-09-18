from __future__ import annotations

import argparse
import json
from pathlib import Path

from pkas.backup_manager import BackupManager
from pkas.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify or restore a PKAS recovery bundle.")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--target-data-root", type=Path)
    args = parser.parse_args()
    manager = BackupManager(get_settings())
    try:
        if args.target_data_root:
            result = manager.restore_bundle(args.bundle, args.target_data_root)
        else:
            result = manager.verify_bundle(args.bundle)
    except Exception as exc:
        result = {"status": "failed", "error_code": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["status"] in {"verified", "restored"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
