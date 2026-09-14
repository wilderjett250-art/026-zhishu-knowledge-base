from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from pkas.db import Database


class ProfileError(RuntimeError):
    pass


class ProfileNotFound(ProfileError):
    pass


class ProfileConflict(ProfileError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CapabilityProfileService:
    """Stores client-neutral capability policy without client config or secrets."""

    schema_version = "pkas.capability-profile.v1"
    asset_kinds = {"skill", "mcp_server"}

    def __init__(self, database: Database) -> None:
        self.database = database

    def list_profiles(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM capability_profiles ORDER BY name COLLATE NOCASE, id"
            ).fetchall()
            return [self._record(connection, row) for row in rows]

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM capability_profiles WHERE id=?", (profile_id,)
            ).fetchone()
            if row is None:
                raise ProfileNotFound("Profile 不存在。")
            return self._record(connection, row)

    def create_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize(payload)
        profile_id = f"profile_{uuid.uuid4().hex}"
        timestamp = _now()
        try:
            with self.database.connect() as connection:
                connection.execute(
                    """INSERT INTO capability_profiles(
                    id, name, description, client_id, status,
                    knowledge_domains_json, allowed_privacy_json,
                    daily_input_token_budget, daily_output_token_budget,
                    revision, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        profile_id,
                        normalized["name"],
                        normalized["description"],
                        normalized["client_id"],
                        normalized["status"],
                        json.dumps(normalized["knowledge_domains"], ensure_ascii=False),
                        json.dumps(normalized["allowed_privacy"], ensure_ascii=False),
                        normalized["daily_input_token_budget"],
                        normalized["daily_output_token_budget"],
                        timestamp,
                        timestamp,
                    ),
                )
                self._replace_bindings(connection, profile_id, normalized["bindings"], timestamp)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            if "capability_profiles.name" in str(exc):
                raise ProfileConflict("Profile 名称已存在。") from exc
            raise
        return self.get_profile(profile_id)

    def replace_profile(
        self,
        profile_id: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        normalized = self._normalize(payload)
        timestamp = _now()
        try:
            with self.database.connect() as connection:
                cursor = connection.execute(
                    """UPDATE capability_profiles SET
                    name=?, description=?, client_id=?, status=?,
                    knowledge_domains_json=?, allowed_privacy_json=?,
                    daily_input_token_budget=?, daily_output_token_budget=?,
                    revision=revision+1, updated_at=?
                    WHERE id=? AND revision=?""",
                    (
                        normalized["name"],
                        normalized["description"],
                        normalized["client_id"],
                        normalized["status"],
                        json.dumps(normalized["knowledge_domains"], ensure_ascii=False),
                        json.dumps(normalized["allowed_privacy"], ensure_ascii=False),
                        normalized["daily_input_token_budget"],
                        normalized["daily_output_token_budget"],
                        timestamp,
                        profile_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount == 0:
                    exists = connection.execute(
                        "SELECT revision FROM capability_profiles WHERE id=?", (profile_id,)
                    ).fetchone()
                    if exists is None:
                        raise ProfileNotFound("Profile 不存在。")
                    raise ProfileConflict("Profile 已被其他操作修改，请刷新后重试。")
                self._replace_bindings(connection, profile_id, normalized["bindings"], timestamp)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            if "capability_profiles.name" in str(exc):
                raise ProfileConflict("Profile 名称已存在。") from exc
            raise
        return self.get_profile(profile_id)

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        keys: set[tuple[str, str]] = set()
        normalized_bindings: list[dict[str, Any]] = []
        for binding in payload.get("bindings", []):
            kind = str(binding["asset_kind"])
            asset_id = str(binding["asset_id"]).strip()
            if kind not in self.asset_kinds:
                raise ProfileError(f"不支持的 Profile 资产类型：{kind}")
            key = (kind, asset_id)
            if key in keys:
                raise ProfileError("Profile 不能重复绑定同一资产。")
            keys.add(key)
            normalized_bindings.append(
                {"asset_kind": kind, "asset_id": asset_id, "enabled": bool(binding["enabled"])}
            )
        return {
            **payload,
            "name": str(payload["name"]).strip(),
            "description": str(payload.get("description", "")).strip(),
            "client_id": payload.get("client_id") or None,
            "knowledge_domains": sorted(set(payload.get("knowledge_domains", []))),
            "allowed_privacy": sorted(set(payload.get("allowed_privacy", []))),
            "bindings": normalized_bindings,
        }

    @staticmethod
    def _replace_bindings(
        connection: sqlite3.Connection,
        profile_id: str,
        bindings: list[dict[str, Any]],
        timestamp: str,
    ) -> None:
        connection.execute(
            "DELETE FROM capability_profile_bindings WHERE profile_id=?", (profile_id,)
        )
        connection.executemany(
            """INSERT INTO capability_profile_bindings(
            profile_id, asset_kind, asset_id, enabled, created_at
            ) VALUES(?, ?, ?, ?, ?)""",
            [
                (
                    profile_id,
                    binding["asset_kind"],
                    binding["asset_id"],
                    int(binding["enabled"]),
                    timestamp,
                )
                for binding in bindings
            ],
        )

    def _record(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        bindings = connection.execute(
            """SELECT asset_kind, asset_id, enabled
            FROM capability_profile_bindings
            WHERE profile_id=? ORDER BY asset_kind, asset_id""",
            (row["id"],),
        ).fetchall()
        return {
            "schema_version": self.schema_version,
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "client_id": row["client_id"],
            "status": row["status"],
            "knowledge_domains": json.loads(row["knowledge_domains_json"]),
            "allowed_privacy": json.loads(row["allowed_privacy_json"]),
            "daily_input_token_budget": row["daily_input_token_budget"],
            "daily_output_token_budget": row["daily_output_token_budget"],
            "bindings": [
                {
                    "asset_kind": item["asset_kind"],
                    "asset_id": item["asset_id"],
                    "enabled": bool(item["enabled"]),
                }
                for item in bindings
            ],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "secret_values_returned": False,
        }
