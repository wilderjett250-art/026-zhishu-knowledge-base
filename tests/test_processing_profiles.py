from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.processing_profiles import (
    ProcessingProfileUpdate,
    load_processing_profile,
    save_processing_profile,
)


def test_processing_profiles_default_to_custom_for_a_fresh_machine(test_settings) -> None:
    data = load_processing_profile(test_settings)
    assert data["profile_id"] == "custom"
    assert data["configured"] is False
    assert data["rules"]["markdown"] == "full"
    assert data["rules"]["code"] == "catalog"
    assert data["selection_flow"] == "ai_review_then_confirm"
    assert data["requires_ai_review"] is True


def test_processing_profile_preset_and_custom_rules_are_persisted(test_settings) -> None:
    saved = save_processing_profile(
        test_settings,
        ProcessingProfileUpdate(profile_id="work_efficiency"),
    )
    assert saved["profile_id"] == "work_efficiency"
    assert saved["rules"]["markdown"] == "semantic"
    assert saved["rules"]["code"] == "catalog"

    custom = save_processing_profile(
        test_settings,
        ProcessingProfileUpdate(
            profile_id="custom",
            rules={
                "markdown": "semantic",
                "documents": "full",
                "code": "catalog",
                "images": "catalog",
                "other": "exclude",
            },
        ),
        )
    assert custom["profile_id"] == "custom"
    assert custom["rules"]["other"] == "exclude"
    assert custom["configured"] is True
    assert custom["selection_flow"] == "ai_review_then_confirm"


def test_processing_profile_api_lists_four_options_and_saves_without_scanning(
    test_settings,
) -> None:
    with TestClient(create_app(test_settings)) as client:
        listed = client.get("/api/foundation/processing-profiles")
        assert listed.status_code == 200
        payload = listed.json()["data"]
        assert payload["first_run_required"] is True
        assert [item["profile_id"] for item in payload["profiles"]] == [
            "work_efficiency",
            "complete_personal",
            "lightweight",
            "custom",
        ]
        assert payload["profiles"][-1]["requires_ai_review"] is True

        saved = client.put(
            "/api/foundation/processing-profile",
            json={"profile_id": "custom", "rules": payload["current"]["rules"]},
        )
        assert saved.status_code == 200
        assert saved.json()["data"]["profile_id"] == "custom"
        assert saved.json()["data"]["configured"] is True

        sources = client.get("/api/foundation/auto-sources")
        assert sources.status_code == 200
        assert sources.json()["data"]["requires_path_input"] is False
        assert sources.json()["data"]["scan_started"] is False

        assert not (test_settings.data_root / "intake").exists()
