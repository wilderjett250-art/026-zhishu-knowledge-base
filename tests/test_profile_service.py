import pytest

from pkas.db import Database
from pkas.profile_service import CapabilityProfileService, ProfileConflict, ProfileError


def _payload() -> dict[str, object]:
    return {
        "name": "开发工作台",
        "description": "供编码客户端使用的最小能力集合",
        "client_id": "codex",
        "status": "active",
        "knowledge_domains": ["work"],
        "allowed_privacy": ["private", "public"],
        "daily_input_token_budget": 120000,
        "daily_output_token_budget": 12000,
        "bindings": [
            {"asset_kind": "skill", "asset_id": "python-review", "enabled": True},
            {
                "asset_kind": "mcp_server",
                "asset_id": "personal_knowledge",
                "enabled": False,
            },
        ],
    }


def test_profile_create_list_and_revision_guard(test_settings) -> None:
    database = Database(test_settings)
    database.initialize()
    service = CapabilityProfileService(database)

    created = service.create_profile(_payload())
    assert created["revision"] == 1
    assert created["secret_values_returned"] is False
    assert created["knowledge_domains"] == ["work"]
    assert len(created["bindings"]) == 2
    assert service.list_profiles()[0]["id"] == created["id"]

    replacement = {**_payload(), "description": "更新后的策略"}
    updated = service.replace_profile(created["id"], replacement, expected_revision=1)
    assert updated["revision"] == 2
    assert updated["description"] == "更新后的策略"

    with pytest.raises(ProfileConflict):
        service.replace_profile(created["id"], replacement, expected_revision=1)


def test_profile_rejects_duplicate_name_and_binding(test_settings) -> None:
    database = Database(test_settings)
    database.initialize()
    service = CapabilityProfileService(database)
    service.create_profile(_payload())

    with pytest.raises(ProfileConflict):
        service.create_profile(_payload())

    invalid = _payload()
    invalid["name"] = "重复绑定"
    invalid["bindings"] = [
        {"asset_kind": "skill", "asset_id": "same", "enabled": True},
        {"asset_kind": "skill", "asset_id": "same", "enabled": False},
    ]
    with pytest.raises(ProfileError):
        service.create_profile(invalid)
