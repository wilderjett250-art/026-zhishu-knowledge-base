import zipfile

import pytest
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.content_taxonomy import TaxonomyUpdate, classify, load_taxonomy, save_taxonomy
from pkas.directory_summary import (
    ClassificationEdit,
    DirectorySummaryRequest,
    DirectorySummaryService,
)
from pkas.file_inspector import inspect_file


def test_filename_is_not_content_evidence_and_ambiguity_is_preserved(test_settings):
    taxonomy = load_taxonomy(test_settings.data_root)
    assert classify({"name": "合同.docx", "text_preview": "天气很好"}, taxonomy)[
        "category_id"] == "unresolved_other"
    result = classify({"text_preview": "甲方、乙方约定违约责任"}, taxonomy)
    assert result["category_id"] == "finance_contract"
    assert result["review_status"] == "suggested"
    assert result["needs_ai_review"] is True
    assert 0 < result["confidence"] <= 0.85
    conflict = classify({"text_preview": "甲方 会议纪要"}, taxonomy)
    assert conflict["ambiguous"]
    assert conflict["category_id"] == "unresolved_other"
    assert conflict["needs_ai_review"] is True


def test_custom_taxonomy_persists_and_rejects_stale_updates(test_settings):
    original = load_taxonomy(test_settings.data_root)
    update = TaxonomyUpdate(expected_revision=original["revision"], categories=[
        *original["categories"],
        dict(id="garden", name="园艺", parent_id=None, keywords=[]),
        dict(id="garden_sample", name="采样记录", parent_id="garden", keywords=["叶片编号"]),
    ])
    changed = save_taxonomy(test_settings.data_root, update)
    assert classify({"text_preview": "叶片编号：0308"}, changed)["category_id"] == "garden_sample"
    assert load_taxonomy(test_settings.data_root) == changed
    with pytest.raises(ValueError, match="刷新"):
        save_taxonomy(test_settings.data_root, update)
    with pytest.raises(ValueError, match="两级"):
        TaxonomyUpdate(expected_revision="", categories=[*changed["categories"],
            dict(id="third", name="三级", parent_id="garden_sample", keywords=[])])


def test_disguised_office_reads_content_and_classifies(test_settings, source_root):
    file = source_root / "照片.txt"
    with zipfile.ZipFile(file, "w") as z:
        z.writestr("word/document.xml", '<document><p><t>甲方乙方约定违约责任</t></p></document>')
    record = inspect_file(file)
    assert record["detected_type"] == "docx"
    assert record["type_mismatch"]
    assert "甲方" in record["text_preview"]
    result = classify(record, load_taxonomy(test_settings.data_root))
    assert result["category_id"] == "finance_contract"


def test_manual_correction_survives_reopen_and_keeps_original(
    test_settings, source_root, monkeypatch
):
    file = source_root / "a.txt"
    file.write_text("甲方乙方违约责任", encoding="utf-8")
    monkeypatch.setattr("pkas.directory_summary.everything_scan", lambda *args: iter([file]))
    service = DirectorySummaryService(test_settings)
    plan = service.preview(DirectorySummaryRequest(path=str(source_root)))
    changed = service.edit_classification(plan["id"], ClassificationEdit(
        relative="a.txt", category_id="learning_course", expected_revision=1))
    record = changed["units"][0]["inspected"][0]
    assert record["original_classification"]["category_id"] == "finance_contract"
    assert record["classification"]["review_status"] == "confirmed"
    assert DirectorySummaryService(test_settings).read(plan["id"]) == changed
    with pytest.raises(ValueError, match="刷新"):
        service.edit_classification(plan["id"], ClassificationEdit(
            relative="a.txt", category_id="learning_course", expected_revision=1))
    assert file.read_text(encoding="utf-8") == "甲方乙方违约责任"


def test_no_silent_directory_truncation(test_settings, source_root, monkeypatch):
    files = []
    for folder in ["a", "b"]:
        directory = source_root / folder
        directory.mkdir()
        file = directory / "a.txt"
        file.write_text("hello")
        files.append(file)
    monkeypatch.setattr("pkas.directory_summary.everything_scan", lambda *args: iter(files))
    with pytest.raises(ValueError, match="静默"):
        DirectorySummaryService(test_settings).preview(DirectorySummaryRequest(
            path=str(source_root), max_directories=1))


def test_category_api_roundtrip(test_settings):
    with TestClient(create_app(test_settings)) as client:
        current = client.get("/api/foundation/content-categories").json()["data"]
        assert len([c for c in current["categories"] if not c["parent_id"]]) == 10
        current["categories"][0]["name"] = "我的工作"
        response = client.post("/api/foundation/content-categories", json={
            "categories": current["categories"], "expected_revision": current["revision"]})
        assert response.status_code == 200
        assert client.get("/api/foundation/content-categories").json()["data"][
            "categories"][0]["name"] == "我的工作"


def test_chinese_prefix_cut_does_not_erase_text(source_root):
    file = source_root / "chinese.txt"
    file.write_text("甲方乙方" * 100, encoding="utf-8")
    record = inspect_file(file, preview_bytes=13)
    assert record["text_preview"] == "甲方乙方"
    assert record["coverage"] == "partial"


def test_large_container_is_explicitly_not_fully_read(source_root):
    file = source_root / "large.pdf"
    with file.open("wb") as stream:
        stream.write(b"%PDF-1.7\n")
        stream.truncate(20_000_001)
    record = inspect_file(file)
    assert record["coverage"] == "signature_only"
    assert "20MB" in record["notes"][0]
