"""Create a consistent, verified SQLite backup without exposing database contents."""

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pkas.config import get_settings


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def backup(database_path: Path, output_dir: Path, label: str) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output = output_dir / f"pkas-{label}-{timestamp}.sqlite"
    if output.exists():
        raise FileExistsError(output)
    source = sqlite3.connect(database_path)
    target = sqlite3.connect(output)
    try:
        source.backup(target)
        target.commit()
        quick_check = target.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        target.close()
        source.close()
    if quick_check != "ok":
        output.unlink(missing_ok=True)
        raise RuntimeError("Backup quick_check failed.")
    result = {
        "status": "completed",
        "backup_path": str(output),
        "byte_size": output.stat().st_size,
        "sha256": sha256_file(output),
        "quick_check": quick_check,
    }
    manifest = output.with_suffix(".manifest.json")
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["manifest_path"] = str(manifest)
    return result


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="manual-backup")
    parser.add_argument("--output-dir", type=Path, default=settings.data_root / "backups")
    args = parser.parse_args()
    print(
        json.dumps(
            backup(settings.database_path, args.output_dir, args.label),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
