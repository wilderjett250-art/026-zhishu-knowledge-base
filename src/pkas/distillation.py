import json
from pathlib import Path
from typing import Any

from pkas.config import Settings, get_settings
from pkas.repository import Repository, utc_now


class DistillationService:
    def __init__(
        self,
        settings: Settings | None = None,
        repository: Repository | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository or Repository()

    def export_jsonl(
        self,
        *,
        approved_only: bool = True,
        dataset_split: str | None = None,
    ) -> dict[str, Any]:
        examples = self.repository.approved_distillation_examples(
            approved_only=approved_only,
            dataset_split=dataset_split,
        )
        export_root = self.settings.data_root / "distill" / "exports"
        export_root.mkdir(parents=True, exist_ok=True)
        timestamp = utc_now().replace(":", "-").replace("+", "_")
        output_path = export_root / f"personal-distillation-{timestamp}.jsonl"
        with output_path.open("w", encoding="utf-8", newline="\n") as handle:
            for item in examples:
                record = {
                    "messages": [
                        {"role": "user", "content": item["input_text"]},
                        {"role": "assistant", "content": item["preferred_output"]},
                    ],
                    "metadata": {
                        "id": item["id"],
                        "type": item["example_type"],
                        "rationale": item["rationale"],
                        "source_ids": item["source_ids"],
                        "privacy": item["privacy"],
                        "dataset_split": item["dataset_split"],
                    },
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {
            "path": str(Path(output_path).resolve()),
            "example_count": len(examples),
            "approved_only": approved_only,
            "dataset_split": dataset_split,
        }
