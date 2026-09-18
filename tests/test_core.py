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


def test_project_scoped_search_does_not_cross_recall_same_named_projects(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    project_a = source_root / "project-a"
    project_b = source_root / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    (project_a / "status.md").write_text(
        "项目 Alpha 已完成接口验证。", encoding="utf-8"
    )
    (project_b / "status.md").write_text(
        "项目 Beta 已完成接口验证。", encoding="utf-8"
    )
    first = knowledge_system.ingestion.import_file(
        project_a / "status.md", domain="work", privacy="private"
    )
    second = knowledge_system.ingestion.import_file(
        project_b / "status.md", domain="work", privacy="private"
    )

    scoped = knowledge_system.repository.search(
        "已完成接口验证", domain="work", workspace_path=str(project_a)
    )

    assert scoped
    assert {item["source_id"] for item in scoped} == {first["source_id"]}
    assert all(item["source_id"] != second["source_id"] for item in scoped)


def test_exact_source_name_is_ranked_before_mentions(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    target = source_root / "DEG_project_inventory.md"
    target.write_text("项目台账正文", encoding="utf-8")
    mention = source_root / "conversation.md"
    mention.write_text("请导入 DEG_project_inventory.md", encoding="utf-8")
    imported = knowledge_system.ingestion.import_file(
        target,
        domain="work",
        privacy="private",
    )
    knowledge_system.ingestion.import_file(
        mention,
        domain="work",
        privacy="private",
    )

    results = knowledge_system.repository.search("DEG_project_inventory.md", domain="work")

    assert results[0]["source_id"] == imported["source_id"]
    assert results[0]["match_strategy"] == "exact_source_name"


def test_auto_draft_thread_summaries_stay_out_of_dashboard_and_search(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    source = source_root / "019da6a1-1e8c-7393-86eb-353e074224d3.md"
    source.write_text("# Unity 动作来源排查\n正文可读取。", encoding="utf-8")
    knowledge_system.ingestion.import_file(source, domain="work", privacy="private")
    with knowledge_system.database.connect() as connection:
        connection.execute(
            "UPDATE sources SET source_type='thread-summary' WHERE original_name=?",
            (source.name,),
        )
        connection.execute(
            "UPDATE documents SET title='Unity 动作来源排查' WHERE source_id=("
            "SELECT id FROM sources WHERE original_name=? LIMIT 1)",
            (source.name,),
        )
        connection.commit()

    recent = knowledge_system.repository.stats()["recent_sources"]
    listed = knowledge_system.repository.list_sources(status="indexed")[0]

    assert recent == []
    assert listed["title"] == "Unity 动作来源排查"
    assert listed["document_id"].startswith("doc_")
    assert listed["original_name"] == source.name
    assert knowledge_system.repository.search("正文可读取", domain="work") == []


def test_natural_language_search_broadens_locally_without_losing_provenance(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    note = source_root / "project-status.md"
    note.write_text(
        "PKAS 当前已经完成 Codex 历史增量同步，下一步是接入 DeepSeek Agent。",
        encoding="utf-8",
    )
    knowledge_system.workflows.run_import(
        path=str(note),
        recursive=False,
        domain="work",
        privacy="private",
    )

    results = knowledge_system.repository.search(
        "请核对知识库当前 Codex 历史增量同步和 DeepSeek Agent 接入进度",
        domain="work",
    )

    assert len(results) == 1
    assert results[0]["original_uri"] == str(note.resolve())
    assert results[0]["match_strategy"] == "broad_local"
    assert results[0]["document_id"].startswith("doc_")


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


def test_project_knowledge_candidate_requires_approval_before_search(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    note = source_root / "agent-evidence.md"
    note.write_text("项目已经完成本地全文检索验证。", encoding="utf-8")
    imported = knowledge_system.workflows.run_import(
        path=str(note),
        recursive=False,
        domain="work",
        privacy="private",
    )
    source_id = imported["result"]["items"][0]["source_id"]

    first = knowledge_system.repository.create_knowledge_candidate(
        domain="work",
        knowledge_type="project_state",
        title="PKAS 当前开发状态",
        content="已经完成全文检索，DeepSeek Agent 尚待接入。",
        confidence="high",
        privacy="private",
        evidence_ids=[source_id],
    )
    results = knowledge_system.repository.search("PKAS 当前开发状态", domain="work")
    assert all(result["document_id"] != first["id"] for result in results)
    with knowledge_system.database.connect() as connection:
        connection.execute(
            "UPDATE knowledge_items SET review_status = 'approved' WHERE id = ?",
            (first["id"],),
        )
        connection.commit()
    approved = knowledge_system.repository.search("PKAS 当前开发状态", domain="work")
    item = next(result for result in approved if result["document_id"] == first["id"])
    assert item["match_strategy"] == "knowledge_item"
    assert item["evidence_ids"] == [source_id]
    document = knowledge_system.repository.read_document(first["id"])
    assert document is not None
    assert "DeepSeek Agent" in document["text"]

    second = knowledge_system.repository.create_knowledge_candidate(
        domain="work",
        knowledge_type="project_state",
        title="PKAS 当前开发状态",
        content="全文检索和 DeepSeek Agent 均已接入。",
        confidence="high",
        privacy="private",
        evidence_ids=[source_id],
    )
    assert second["supersedes"] == first["id"]
    active = knowledge_system.repository.search("PKAS 当前开发状态", domain="work")
    assert all(result["document_id"] != second["id"] for result in active)
    assert all(result["document_id"] != first["id"] for result in active)
