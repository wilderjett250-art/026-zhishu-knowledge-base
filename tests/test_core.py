import json
from pathlib import Path

from pkas.system import KnowledgeSystem


def test_import_search_deduplicate_and_provenance(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    note = source_root / "project-note.md"
    note.write_text(
        "门店项目的核心原则是先验证销售宝接口权限，再做自动化工作流。",
        encoding="utf-8",
    )

    inspection = knowledge_system.ingestion.inspect_path(str(source_root), True)
    assert inspection["supported_files"] == 1
    assert inspection["sensitive_files"] == 0

    first = knowledge_system.workflows.run_import(
        path=str(source_root),
        recursive=True,
        domain="work",
        privacy="private",
    )
    assert first["status"] == "completed"
    assert first["result"]["imported"] == 1

    second = knowledge_system.workflows.run_import(
        path=str(source_root),
        recursive=True,
        domain="work",
        privacy="private",
    )
    assert second["result"]["duplicates"] == 1

    results = knowledge_system.repository.search("销售宝接口权限", domain="work")
    assert len(results) == 1
    assert results[0]["original_uri"] == str(note.resolve())
    assert results[0]["document_id"].startswith("doc_")
    assert results[0]["locator"].startswith("paragraph:")
    assert Path(results[0]["vault_path"]).is_file()


def test_restricted_content_requires_explicit_search(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    private_chat = source_root / "chat.jsonl"
    private_chat.write_text(
        json.dumps(
            {
                "conversation_id": "self-1",
                "speaker": "我",
                "timestamp": "2026-08-03T08:00:00+08:00",
                "text": "我习惯在重大技术选择前先看真实运行证据。",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = knowledge_system.workflows.run_import(
        path=str(private_chat),
        recursive=False,
        domain="self",
        privacy="restricted",
    )
    assert result["status"] == "completed"

    hidden = knowledge_system.repository.search("真实运行证据", domain="self")
    visible = knowledge_system.repository.search(
        "真实运行证据",
        domain="self",
        include_restricted=True,
    )
    assert hidden == []
    assert len(visible) == 1

    document = knowledge_system.repository.read_document(visible[0]["document_id"])
    assert document is not None
    assert "重大技术选择" in document["text"]
    assert document["privacy"] == "restricted"


def test_sensitive_files_are_never_imported(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    (source_root / ".env").write_text("TOKEN=do-not-ingest", encoding="utf-8")
    (source_root / "readme.txt").write_text("普通资料", encoding="utf-8")

    inspection = knowledge_system.ingestion.inspect_path(str(source_root), True)
    assert inspection["sensitive_files"] == 1
    assert inspection["supported_files"] == 1

    result = knowledge_system.ingestion.import_path(
        str(source_root),
        domain="work",
        privacy="private",
    )
    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert all(item["path"].endswith("readme.txt") for item in result["items"])


def test_persona_and_distillation_require_review(
    knowledge_system: KnowledgeSystem,
) -> None:
    observation = knowledge_system.repository.create_persona_candidate(
        "preference",
        "偏好先验证再宣布完成。",
        [],
        "high",
    )
    assert observation["approval_status"] == "candidate"
    assert knowledge_system.repository.review_persona(
        observation["id"],
        "approved",
        "用户确认",
    )

    example = knowledge_system.repository.create_distillation_candidate(
        example_type="decision",
        input_text="什么时候可以宣布交付？",
        preferred_output="目标环境完成真实验收后。",
        rejected_output=None,
        rationale="避免把代码完成等同于交付完成",
        source_ids=[],
        privacy="restricted",
    )
    assert example["approval_status"] == "candidate"
    assert knowledge_system.repository.approved_distillation_examples() == []
    assert knowledge_system.repository.review_distillation(
        example["id"],
        "approved",
        "用户确认",
    )

    exported = knowledge_system.distillation.export_jsonl()
    output = Path(exported["path"])
    assert exported["example_count"] == 1
    record = json.loads(output.read_text(encoding="utf-8").strip())
    assert record["messages"][0]["role"] == "user"
    assert record["messages"][1]["content"] == "目标环境完成真实验收后。"
