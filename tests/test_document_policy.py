import fitz
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.document_extraction import DocumentExtractionPipeline
from pkas.document_policy import (
    disable_enhancement,
    document_policy,
    format_capabilities,
    policy_path,
)


def test_local_default_blocks_legacy_extractors(test_settings, source_root, monkeypatch):
    settings = test_settings.model_copy(
        update={
            "document_ai_enhancement_enabled": False,
            "document_docling_enabled": True,
            "document_paddleocr_enabled": True,
            "document_paddleocr_base_url": "https://invalid.example",
            "document_allow_remote_processing": True,
        }
    )
    path = source_root / "mixed.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Local document evidence")
        pdf.new_page()
        pdf.save(path)
    pipeline = DocumentExtractionPipeline(settings=settings)

    def forbidden(*args, **kwargs):
        raise AssertionError("Enhancement must not run in local mode")

    monkeypatch.setattr(pipeline.docling, "parse", forbidden)
    monkeypatch.setattr(pipeline.paddle, "parse_pdf_pages", forbidden)
    result = pipeline.parse(path)
    assert "Local document evidence" in result.text
    assert result.metadata["document_policy"]["mode"] == "local"
    assert result.metadata["extraction"]["warnings"]


def test_saved_off_overrides_legacy_config_and_corruption_fails_closed(test_settings):
    settings = test_settings.model_copy(update={"document_ai_enhancement_enabled": True})
    assert document_policy(settings)["ai_enhancement_enabled"]
    assert not disable_enhancement(settings)["ai_enhancement_enabled"]
    assert not document_policy(settings)["ai_enhancement_enabled"]
    policy_path(settings).write_text("broken", encoding="utf-8")
    status = document_policy(settings)
    assert not status["ai_enhancement_enabled"]
    assert status["warning"]


def test_format_capabilities_match_local_parser_boundaries():
    capabilities = format_capabilities()
    native = {
        extension
        for group in capabilities["native_groups"]
        for extension in group["extensions"]
    }
    assert {".rtf", ".odt", ".ods", ".odp", ".docx", ".xlsx", ".pptx", ".pdf"} <= native
    assert set(capabilities["local_converter_formats"]) == {".doc", ".xls", ".ppt"}
    assert ".msg" in capabilities["optional_converter_formats"]
    assert capabilities["local_converter_formats"] == [".doc", ".ppt", ".xls"]
    assert ".pdf" in capabilities["visual_review_formats"]


def test_api_default_and_persistence_without_fake_luna(test_settings):
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/settings/document-parsing")
        assert response.status_code == 200
        assert response.json()["data"]["mode"] == "local"
        native_groups = response.json()["data"]["format_capabilities"]["native_groups"]
        assert ".odt" in native_groups[0]["extensions"]
        response = client.post(
            "/api/settings/document-parsing", json={"ai_enhancement_enabled": True}
        )
        assert response.status_code == 409
        assert not policy_path(test_settings).exists()
        response = client.post(
            "/api/settings/document-parsing", json={"ai_enhancement_enabled": False}
        )
        assert response.status_code == 200
        assert not response.json()["data"]["ai_enhancement_enabled"]
        assert (
            client.post(
                "/api/settings/document-parsing", json={"ai_enhancement_enabled": "false"}
            ).status_code
            == 422
        )
    with TestClient(create_app(test_settings)) as client:
        assert client.get("/api/settings/document-parsing").json()["data"]["mode"] == "local"
