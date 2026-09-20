from __future__ import annotations

import json

from pkas.system import KnowledgeSystem


def main() -> None:
    system = KnowledgeSystem.create()
    result = system.repository.run_codex_turn_user_task_migration()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
