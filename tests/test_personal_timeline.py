import json
from datetime import datetime

import pytest

from pkas.personal_timeline import PersonalTimelineService


def _seed(knowledge_system, *, text: str, sent_at: int, is_self: int = 1) -> str:
    now = datetime.now().astimezone().isoformat()
    with knowledge_system.database.connect() as connection:
        connection.execute(
            """INSERT OR IGNORE INTO customers(
            id,platform,platform_id,display_name,customer_type,stage,tags_json,
            privacy,review_status,metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "cust_timeline", "wechat", "cust-platform", "候选联系人",
                "unknown", "active", "[]", "restricted", "candidate", "{}", now, now,
            ),
        )
        connection.execute(
            """INSERT OR IGNORE INTO customer_conversations(
            id,customer_id,platform,platform_id,name,conversation_type,privacy,
            metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "conv_timeline", "cust_timeline", "wechat", "conv-platform",
                "项目群", "group", "restricted", "{}", now, now,
            ),
        )
        message_id = f"msg_{sent_at}_{is_self}"
        connection.execute(
            """INSERT INTO customer_messages(
            id,conversation_id,is_self,sent_at,message_type,content,source_hash,
            privacy,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                message_id, "conv_timeline", is_self, sent_at, "text", text,
                message_id, "restricted", now,
            ),
        )
        connection.commit()
    return message_id


def test_candidate_selection_only_uses_self_and_removes_noise(knowledge_system):
    service = PersonalTimelineService(
        knowledge_system.database, knowledge_system.settings
    )
    local_now = datetime.now().astimezone()
    day = local_now.date().isoformat()
    base = int(local_now.replace(hour=10, minute=0, second=0).timestamp())
    kept = _seed(
        knowledge_system,
        text="今天继续调试知识库检索功能",
        sent_at=base,
    )
    _seed(knowledge_system, text="好的", sent_at=base + 1)
    _seed(
        knowledge_system,
        text="客户说已经完成部署",
        sent_at=base + 2,
        is_self=0,
    )
    total, candidates = service._messages_for_day(day)
    assert total == 2
    assert [item["id"] for item in candidates] == [kept]


def test_readiness_only_lists_self_message_days_without_summaries(knowledge_system):
    service = PersonalTimelineService(
        knowledge_system.database, knowledge_system.settings
    )
    local_now = datetime.now().astimezone()
    day = local_now.date().isoformat()
    _seed(
        knowledge_system,
        text="今天继续整理知识库资料",
        sent_at=int(local_now.replace(hour=11, minute=0, second=0).timestamp()),
    )

    readiness = service.readiness(7)

    assert readiness["mode"] == "manual_luna_build"
    assert readiness["pending_day_count"] == 1
    assert readiness["pending_days"][0]["local_date"] == day
    assert readiness["pending_days"][0]["message_count"] == 1
    assert readiness["pending_days"][0]["pending_reason"] == "missing_summary"


def test_readiness_marks_a_day_stale_after_new_self_messages(knowledge_system):
    service = PersonalTimelineService(
        knowledge_system.database, knowledge_system.settings
    )
    local_now = datetime.now().astimezone()
    day = local_now.date().isoformat()
    base = int(local_now.replace(hour=9, minute=0, second=0).timestamp())
    _seed(knowledge_system, text="今天处理项目资料", sent_at=base)
    now = datetime.now().astimezone().isoformat()
    with knowledge_system.database.connect() as connection:
        connection.execute(
            """INSERT INTO personal_daily_summaries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (day, "旧整理", "[]", "[]", 1, 1, 1, "test", "fake", "unreviewed", now, now),
        )
        connection.commit()

    assert service.readiness(7)["pending_day_count"] == 0
    _seed(knowledge_system, text="下午继续核对导入结果", sent_at=base + 3600)

    readiness = service.readiness(7)
    assert readiness["pending_day_count"] == 1
    assert readiness["pending_days"][0]["pending_reason"] == "new_self_messages"
    assert readiness["pending_days"][0]["summary_source_message_count"] == 1
    assert readiness["pending_days"][0]["message_count"] == 2


def test_validation_rejects_unknown_evidence():
    result = {
        "factual_summary": "处理项目",
        "facts": [
            {
                "statement": "完成项目",
                "activity_type": "work",
                "activity_status": "done",
                "confidence": "high",
                "evidence_ids": ["unknown"],
            }
        ],
        "inferred_focus": [],
        "open_items": [],
    }
    with pytest.raises(ValueError, match="证据"):
        PersonalTimelineService._validated(result, {"known"})


def test_get_day_keeps_fact_and_inference_separate(knowledge_system):
    service = PersonalTimelineService(
        knowledge_system.database, knowledge_system.settings
    )
    now = datetime.now().astimezone().isoformat()
    day = "2026-09-10"
    with knowledge_system.database.connect() as connection:
        connection.execute(
            """INSERT INTO personal_daily_summaries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                day, "处理知识库", json.dumps([{"statement": "可能在忙检索"}]),
                "[]", 10, 3, 1, "test", "fake", "unreviewed", now, now,
            ),
        )
        connection.execute(
            """INSERT INTO personal_activity_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "activity_1", day, "work", "修改检索", "in_progress", "fact",
                "high", '["m1"]', '["c1"]', "test", "fake", "unreviewed", now, now,
            ),
        )
        connection.commit()
    result = service.get_day(day)
    assert result is not None
    assert result["activities"][0]["certainty"] == "fact"
    assert result["inferred_focus"][0]["statement"] == "可能在忙检索"
