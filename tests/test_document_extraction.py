import json
import zipfile
from pathlib import Path
from typing import Any

import fitz
import pytest
from openpyxl import Workbook

from pkas.config import Settings
from pkas.extraction_eval import text_metrics
from pkas.ingest import chunk_document
from pkas.parsers import ParsedBlock, ParsedDocument, ParseError, parse_file
from pkas.system import KnowledgeSystem


def test_jsonl_message_blocks_preserve_time_and_speaker(
    test_settings: Settings,
    source_root: Path,
) -> None:
    path = source_root / "messages.jsonl"
    records = [
        {
            "sent_at": "2026-08-07 10:00:00",
            "speaker": "客户甲",
            "text": "需要更新项目排期",
        }
    ]
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        encoding="utf-8",
    )

    parsed = parse_file(path, settings=test_settings)
    chunks = chunk_document(parsed, max_chars=200)

    expected = "[2026-08-07 10:00:00 · 客户甲] 需要更新项目排期"
    assert parsed.blocks[0].text == expected
    assert chunks[0]["text"] == expected


def test_pdf_records_low_text_pages_and_keeps_native_provenance(
    test_settings: Settings,
    source_root: Path,
) -> None:
    path = source_root / "mixed.pdf"
    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), "native searchable project evidence")
    document.new_page()
    document.save(path)
    document.close()

    parsed = parse_file(path, settings=test_settings)

    extraction = parsed.metadata["extraction"]
    assert parsed.parser_name == "pymupdf-blocks"
    assert "native searchable project evidence" in parsed.text
    assert extraction["initial_quality"]["visual_pages"] == [2]
    assert extraction["warnings"] == ["paddleocr:remote_processing_not_enabled"]
    assert parsed.blocks[0].locator.startswith("page:1/block:")


def test_pdf_suppresses_repeated_short_headers_but_keeps_page_body(
    test_settings: Settings,
    source_root: Path,
) -> None:
    path = source_root / "repeated-header.pdf"
    document = fitz.open()
    for page_number in range(1, 5):
        page = document.new_page()
        page.insert_text((72, 40), "CONFIDENTIAL PROJECT HEADER")
        page.insert_text((72, 160), f"Unique body evidence page {page_number}")
    document.save(path)
    document.close()

    parsed = parse_file(path, settings=test_settings)

    assert "CONFIDENTIAL PROJECT HEADER" not in parsed.text
    assert all(f"Unique body evidence page {page}" in parsed.text for page in range(1, 5))
    assert parsed.metadata["suppressed_repeated_margin_blocks"] == 4


def test_paddleocr_service_fills_scanned_pdf_page_when_explicitly_enabled(
    test_settings: Settings,
    source_root: Path,
) -> None:
    path = source_root / "scan.pdf"
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()
    settings = Settings(
        project_root=test_settings.project_root,
        data_root=test_settings.data_root,
        allowed_origins=[],
        document_paddleocr_enabled=True,
        document_ai_enhancement_enabled=True,
        document_paddleocr_base_url="http://127.0.0.1:8080",
        document_allow_remote_processing=True,
    )
    payloads: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        payloads.append(payload)
        return {
            "result": {
                "layoutParsingResults": [
                    {
                        "prunedResult": {
                            "parsing_res_list": [
                                {
                                    "block_label": "text",
                                    "block_content": "扫描页识别文字",
                                    "block_bbox": [10, 20, 200, 80],
                                }
                            ]
                        }
                    }
                ]
            }
        }

    parsed = parse_file(path, settings=settings, paddle_transport=transport)

    assert parsed.text == "扫描页识别文字"
    assert "paddleocr-vl-1.6-service" in parsed.parser_name
    assert parsed.blocks[0].locator == "page:1/visual-block:1"
    assert parsed.blocks[0].bbox == (10.0, 20.0, 200.0, 80.0)
    assert payloads[0]["fileType"] == 1
    assert payloads[0]["visualize"] is False


def test_restricted_document_is_not_sent_to_remote_parser(
    test_settings: Settings,
    source_root: Path,
) -> None:
    path = source_root / "restricted-scan.pdf"
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()
    settings = Settings(
        project_root=test_settings.project_root,
        data_root=test_settings.data_root,
        allowed_origins=[],
        document_paddleocr_enabled=True,
        document_ai_enhancement_enabled=True,
        document_paddleocr_base_url="http://127.0.0.1:8080",
        document_allow_remote_processing=True,
    )
    called = False

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    with pytest.raises(ParseError, match="没有可索引文字"):
        parse_file(
            path,
            settings=settings,
            privacy="restricted",
            paddle_transport=transport,
        )
    assert called is False


def test_pptx_openxml_parser_extracts_slide_table_and_notes(
    test_settings: Settings,
    source_root: Path,
) -> None:
    path = source_root / "project.pptx"
    slide_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
           xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld><p:spTree>
        <p:sp><p:txBody><a:p><a:r><a:t>项目总览</a:t></a:r></a:p></p:txBody></p:sp>
        <a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>关键指标</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl>
      </p:spTree></p:cSld>
    </p:sld>"""
    notes_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
             xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>讲解备注</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
    </p:notes>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide_xml)
        archive.writestr("ppt/notesSlides/notesSlide1.xml", notes_xml)

    parsed = parse_file(path, settings=test_settings)

    assert "项目总览" in parsed.text
    assert "关键指标" in parsed.text
    assert "讲解备注" in parsed.text
    assert parsed.metadata["slide_count"] == 1
    assert {block.kind for block in parsed.blocks} >= {"heading", "table-row", "speaker-note"}


def test_xlsx_parser_preserves_formula_and_cell_coordinates(
    test_settings: Settings,
    source_root: Path,
) -> None:
    path = source_root / "inventory.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "项目台账"
    sheet["A1"] = "数量"
    sheet["A2"] = 2
    sheet["B2"] = "=A2*3"
    workbook.save(path)

    parsed = parse_file(path, settings=test_settings)

    assert "A1=数量" in parsed.text
    assert "B2=[公式 =A2*3]" in parsed.text
    assert parsed.metadata["formula_count"] == 1
    assert any(block.locator == "sheet:项目台账/row:2" for block in parsed.blocks)
    table_rows = [block for block in parsed.blocks if block.kind == "table-row"]
    assert table_rows[0].metadata["is_header"] is True
    assert table_rows[1].metadata["table_id"] == "sheet:项目台账"


def test_semantic_chunking_preserves_block_locators() -> None:
    parsed = ParsedDocument(
        title="结构化文档",
        text="标题\n内容",
        parser_name="test",
        blocks=[
            ParsedBlock(kind="heading", text="标题", locator="page:1/block:1", page=1),
            ParsedBlock(kind="paragraph", text="内容", locator="page:1/block:2", page=1),
        ],
    )

    chunks = chunk_document(parsed, max_chars=100)

    assert chunks == [
        {
            "sequence": 0,
            "text": "标题\n\n内容",
            "locator": "page:1/block:1..page:1/block:2",
            "block_sequences": [0, 1],
            "chunk_kind": "document-section",
            "chunker_version": "typed-v2",
        }
    ]


def test_typed_chunking_uses_table_message_and_code_profiles() -> None:
    table = ParsedDocument(
        title="台账",
        text="",
        parser_name="test",
        blocks=[
            ParsedBlock(kind="heading", text="项目台账", locator="sheet:台账"),
            ParsedBlock(kind="table-row", text="编号 | 状态", locator="row:1"),
            *[
                ParsedBlock(kind="table-row", text=f"{index} | 完成", locator=f"row:{index}")
                for index in range(2, 47)
            ],
        ],
    )
    messages = ParsedDocument(
        title="会话",
        text="",
        parser_name="test",
        blocks=[
            ParsedBlock(kind="message", text=f"用户：消息 {index}", locator=f"record:{index}")
            for index in range(35)
        ],
    )
    code = ParsedDocument(
        title="service",
        text="",
        parser_name="plain-text",
        metadata={"source_extension": ".py"},
        blocks=[
            ParsedBlock(kind="paragraph", text=f"value_{index} = {index}", locator=f"line:{index}")
            for index in range(180)
        ],
    )

    table_chunks = chunk_document(table)
    message_chunks = chunk_document(messages)
    code_chunks = chunk_document(code)

    assert len(table_chunks) >= 3
    assert all(chunk["chunk_kind"] == "table-rows" for chunk in table_chunks)
    assert all("编号 | 状态" in chunk["text"] for chunk in table_chunks)
    assert len(message_chunks) == 2
    assert all(chunk["chunk_kind"] == "message-window" for chunk in message_chunks)
    assert all(len(chunk["block_sequences"]) <= 30 for chunk in message_chunks)
    assert len(code_chunks) >= 2
    assert all(chunk["chunk_kind"] == "code" for chunk in code_chunks)


def test_typed_v2_uses_each_table_own_header() -> None:
    parsed = ParsedDocument(
        title="多表格",
        text="",
        parser_name="test",
        blocks=[
            ParsedBlock(
                kind="table-row",
                text="项目 | 状态",
                locator="table:1/row:1",
                metadata={"table_id": "1", "is_header": True},
            ),
            ParsedBlock(
                kind="table-row",
                text="A | 完成",
                locator="table:1/row:2",
                metadata={"table_id": "1"},
            ),
            ParsedBlock(
                kind="table-row",
                text="设备 | 型号",
                locator="table:2/row:1",
                metadata={"table_id": "2", "is_header": True},
            ),
            ParsedBlock(
                kind="table-row",
                text="主控 | ESP32",
                locator="table:2/row:2",
                metadata={"table_id": "2"},
            ),
        ],
    )

    chunks = chunk_document(parsed, max_chars=30)

    assert any(
        "项目 | 状态" in item["text"] and "A | 完成" in item["text"] for item in chunks
    )
    assert any(
        "设备 | 型号" in item["text"] and "主控 | ESP32" in item["text"] for item in chunks
    )
    assert not any(
        "项目 | 状态" in item["text"] and "主控 | ESP32" in item["text"] for item in chunks
    )


def test_ingested_chunks_have_resolvable_block_relations(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    note = source_root / "block-model.md"
    note.write_text("# 架构\n\n第一段。\n第二段。", encoding="utf-8")

    imported = knowledge_system.ingestion.import_file(
        note,
        domain="work",
        privacy="private",
    )

    with knowledge_system.database.connect() as connection:
        blocks = connection.execute(
            "SELECT COUNT(*) FROM blocks WHERE document_id=?",
            (imported["document_id"],),
        ).fetchone()[0]
        orphans = connection.execute(
            """SELECT COUNT(*) FROM chunks c
            LEFT JOIN chunk_blocks cb ON cb.chunk_id=c.id
            WHERE c.document_id=? AND cb.block_id IS NULL""",
            (imported["document_id"],),
        ).fetchone()[0]
        versions = connection.execute(
            "SELECT DISTINCT chunker_version FROM chunks WHERE document_id=?",
            (imported["document_id"],),
        ).fetchall()
    assert blocks >= 2
    assert orphans == 0
    assert [row[0] for row in versions] == ["typed-v2"]


def test_text_metrics_report_recall_and_precision_separately() -> None:
    metrics = text_metrics("abc", "abx")
    assert metrics == {
        "text_recall": 0.666667,
        "text_precision": 0.666667,
        "text_f1": 0.666667,
    }


def test_reindex_outdated_preserves_source_and_document_identity(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    note = source_root / "architecture.md"
    note.write_text("# 系统架构\n\n混合解析保留来源定位。", encoding="utf-8")
    imported = knowledge_system.ingestion.import_file(
        note,
        domain="work",
        privacy="private",
    )
    source_id = imported["source_id"]
    document_id = imported["document_id"]
    with knowledge_system.database.connect() as connection:
        connection.execute(
            "UPDATE documents SET parser_version = '1' WHERE id = ?",
            (document_id,),
        )
        connection.commit()

    result = knowledge_system.ingestion.reindex_outdated()
    document = knowledge_system.repository.read_document(document_id)
    search = knowledge_system.repository.search("混合解析保留来源定位", domain="work")

    assert result["status"] == "completed"
    assert result["reindexed"] == 1
    assert document is not None
    assert document["source_id"] == source_id
    assert document["title"] == "architecture"
    assert document["metadata"]["extraction"]["pipeline_version"] == "hybrid-v1"
    assert search[0]["document_id"] == document_id
