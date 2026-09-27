from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.clock import FrozenClock, to_storage
from app.core.errors import ConflictError
from app.temple.repository import TempleRepository
from app.temple.rules import DEFAULT_RULES
from app.temple.schema import ensure_temple_schema
from app.temple.service import TempleSafetyService

V1_RULES = DEFAULT_RULES
V2_RULES = {**DEFAULT_RULES, "allocation": {**DEFAULT_RULES["allocation"], "duration_seconds": 240}}
V3_RULES = {**DEFAULT_RULES, "allocation": {**DEFAULT_RULES["allocation"], "duration_seconds": 300}}

TEMPLE_PAYLOAD = {
    "code": "fayun-temple",
    "name": "法云寺",
    "temple_type": "heritage",
    "timezone": "Asia/Shanghai",
    "max_concurrent_mitigation_sessions": 10,
    "ventilation_capacity": 3000,
}
PROFILE_PAYLOAD = {
    "incense_code": "ceremony-incense",
    "name": "法会香火",
    "activity_type": "ceremony",
    "pm25_target": 100,
    "co_target": 0.01,
    "min_supply_airflow": 8,
    "min_exhaust_airflow": 4,
    "default_risk_priority": 70,
}


def observation_payload(*, key: str, observed_at: datetime, steward: str = "steward-ceremony-0001") -> dict:
    return {
        "observation_key": key,
        "temple_code": "fayun-temple",
        "hall_code": "main-hall",
        "incense_code": "ceremony-incense",
        "steward_hash": steward,
        "sensor_class": "ceiling-sensor",
        "visitor_density": 300,
        "pm25_ugm3": 350,
        "co_ppm": 0.08,
        "supply_airflow": 1.5,
        "exhaust_airflow": 0.5,
        "observed_at": to_storage(observed_at),
    }


@pytest.fixture()
def service(tmp_path: Path) -> TempleSafetyService:
    import os
    os.environ["TEMPLE_DATABASE_PATH"] = str(tmp_path / "timeline.db")
    from app.database import close_connection
    close_connection()
    clock = FrozenClock(datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
    instance = TempleSafetyService(clock=clock)
    instance.create_temple(TEMPLE_PAYLOAD)
    instance.add_hall("fayun-temple", {"code": "main-hall", "name": "大雄宝殿", "visit_order": 1, "expected_visit_seconds": 900, "ventilation_capacity": 1200})
    instance.create_incense_profile(PROFILE_PAYLOAD)
    return instance


def publish(service: TempleSafetyService, rules: dict, effective_from: datetime, actor: str = "supervisor-a") -> dict:
    draft = service.create_safety_policy("fayun-temple", rules, actor)
    return service.publish_safety_policy(draft["id"], actor, to_storage(effective_from))


def add_authorization(service: TempleSafetyService, source: str, steward: str = "steward-ceremony-0001") -> None:
    service.add_authorization({
        "steward_hash": steward,
        "temple_code": "fayun-temple",
        "authorization_code": "ceremony-duty",
        "valid_from": to_storage(datetime(2026, 9, 1, tzinfo=UTC)),
        "valid_until": to_storage(datetime(2026, 12, 1, tzinfo=UTC)),
        "source_approval_id": source,
    })


def test_future_publication_is_only_a_reservation(service: TempleSafetyService):
    v1 = publish(service, V1_RULES, datetime(2026, 9, 1, tzinfo=UTC))
    v2 = publish(service, V2_RULES, datetime(2026, 10, 1, tzinfo=UTC))
    # 9-30 的现行规则不变，新规则只是预约。
    current = service.effective_safety_policy("fayun-temple")
    assert current["id"] == v1["id"]
    assert service.effective_safety_policy("fayun-temple", "2026-09-30T23:59:59Z")["id"] == v1["id"]
    connection = service.connection
    old = connection.execute("SELECT state,effective_to FROM safety_policy_versions WHERE id=?", (v1["id"],)).fetchone()
    new = connection.execute("SELECT state,effective_to FROM safety_policy_versions WHERE id=?", (v2["id"],)).fetchone()
    # 旧版保持发布，但已写入可审计的结束边界；新版开放结束边界等待接班。
    assert old["state"] == "published"
    assert old["effective_to"] == "2026-10-01T00:00:00+00:00"
    assert new["state"] == "published"
    assert new["effective_to"] is None


def test_switch_at_boundary_is_unique_and_continuous(service: TempleSafetyService):
    v1 = publish(service, V1_RULES, datetime(2026, 9, 1, tzinfo=UTC))
    v2 = publish(service, V2_RULES, datetime(2026, 10, 1, tzinfo=UTC))
    v3 = publish(service, V3_RULES, datetime(2026, 10, 15, tzinfo=UTC))
    # 半开区间：边界点整分切换到新版，边界前一秒仍是旧版。
    assert service.effective_safety_policy("fayun-temple", "2026-09-30T23:59:59Z")["id"] == v1["id"]
    assert service.effective_safety_policy("fayun-temple", "2026-10-01T00:00:00Z")["id"] == v2["id"]
    assert service.effective_safety_policy("fayun-temple", "2026-10-14T23:59:59Z")["id"] == v2["id"]
    assert service.effective_safety_policy("fayun-temple", "2026-10-15T00:00:00Z")["id"] == v3["id"]
    # 到达约定时点后旧版退役，但历史时点仍可审计地解析到旧版。
    service.clock.current = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
    retired = service.retire_due_safety_policies()
    assert set(retired["retired"]) == {v1["id"], v2["id"]}
    assert service.effective_safety_policy("fayun-temple", "2026-09-30T12:00:00Z")["id"] == v1["id"]
    assert service.effective_safety_policy("fayun-temple", "2026-10-05T00:00:00Z")["id"] == v2["id"]
    assert service.effective_safety_policy("fayun-temple")["id"] == v3["id"]
    retired_rows = service.connection.execute(
        "SELECT id,retired_at IS NOT NULL AS has_boundary FROM safety_policy_versions WHERE state='retired' ORDER BY id"
    ).fetchall()
    assert {row["id"] for row in retired_rows} == {v1["id"], v2["id"]}
    assert all(row["has_boundary"] for row in retired_rows)


def test_timeline_has_no_gap_or_overlap(service: TempleSafetyService):
    publish(service, V1_RULES, datetime(2026, 9, 1, tzinfo=UTC))
    publish(service, V2_RULES, datetime(2026, 10, 1, tzinfo=UTC))
    publish(service, V3_RULES, datetime(2026, 10, 15, tzinfo=UTC))
    rows = service.connection.execute(
        "SELECT effective_from,effective_to FROM safety_policy_versions WHERE effective_from IS NOT NULL ORDER BY effective_from"
    ).fetchall()
    # 预约区间首尾相接：前一版的结束边界恰是后一版的生效时点。
    assert rows[0]["effective_to"] == rows[1]["effective_from"]
    assert rows[1]["effective_to"] == rows[2]["effective_from"]
    assert rows[2]["effective_to"] is None
    # 逐小时扫描整个时间轴：任意时刻恰好一个版本可用，切换只发生在约定时点。
    moment = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2026, 10, 20, tzinfo=UTC)
    previous = None
    transitions = []
    while moment <= end:
        policy = service.effective_safety_policy("fayun-temple", to_storage(moment))
        assert policy is not None, to_storage(moment)
        count = service.connection.execute(
            "SELECT COUNT(*) FROM safety_policy_versions "
            "WHERE effective_from<=? AND (effective_to IS NULL OR effective_to>?)",
            (to_storage(moment), to_storage(moment)),
        ).fetchone()[0]
        assert count == 1
        if previous is not None and policy["id"] != previous:
            transitions.append(moment)
        previous = policy["id"]
        moment += timedelta(hours=1)
    assert transitions == [datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 15, tzinfo=UTC)]


def test_publish_is_idempotent_and_same_slot_conflicts(service: TempleSafetyService):
    v1 = publish(service, V1_RULES, datetime(2026, 9, 1, tzinfo=UTC))
    boundary = datetime(2026, 10, 1, tzinfo=UTC)
    v2_draft = service.create_safety_policy("fayun-temple", V2_RULES, "supervisor-a")
    first = service.publish_safety_policy(v2_draft["id"], "supervisor-a", to_storage(boundary))
    # 同一版本、同一时点重复提交：原样返回，不改动时间轴。
    repeated = service.publish_safety_policy(v2_draft["id"], "supervisor-b", to_storage(boundary))
    assert repeated["id"] == first["id"]
    assert repeated["published_by"] == "supervisor-a"
    # 已发布版本改约到别的时点被拒绝。
    with pytest.raises(ConflictError):
        service.publish_safety_policy(v2_draft["id"], "supervisor-a", "2026-10-02T00:00:00Z")
    # 另一名主管在同一时点安排另一版本：冲突拒绝，不产生重叠预约。
    other_draft = service.create_safety_policy("fayun-temple", V3_RULES, "supervisor-b")
    with pytest.raises(ConflictError) as exc_info:
        service.publish_safety_policy(other_draft["id"], "supervisor-b", to_storage(boundary))
    assert exc_info.value.context["safety_policy_id"] == v2_draft["id"]
    assert service.connection.execute(
        "SELECT COUNT(*) FROM safety_policy_versions WHERE state='published' AND effective_from=?",
        (to_storage(boundary),),
    ).fetchone()[0] == 1
    # 既有版本与边界均未受影响。
    assert service.effective_safety_policy("fayun-temple", "2026-09-30T12:00:00Z")["id"] == v1["id"]


def test_two_supervisors_scheduling_same_slot_only_one_wins(service: TempleSafetyService):
    publish(service, V1_RULES, datetime(2026, 9, 1, tzinfo=UTC))
    drafts = [
        service.create_safety_policy("fayun-temple", V2_RULES, "supervisor-a"),
        service.create_safety_policy("fayun-temple", V3_RULES, "supervisor-b"),
    ]
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def schedule(draft: dict, actor: str) -> None:
        # 每个线程使用独立连接，模拟两名主管同时提交。
        from app.database import _create_connection
        connection = _create_connection()
        try:
            rival = TempleSafetyService(connection, service.clock)
            barrier.wait()
            rival.publish_safety_policy(draft["id"], actor, "2026-10-01T00:00:00Z")
        except Exception as exc:  # noqa: BLE001 - 汇总到断言
            errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=schedule, args=(drafts[0], "supervisor-a")),
        threading.Thread(target=schedule, args=(drafts[1], "supervisor-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(errors) == 1
    assert isinstance(errors[0], ConflictError)
    assert service.connection.execute(
        "SELECT COUNT(*) FROM safety_policy_versions WHERE state='published' AND effective_from='2026-10-01T00:00:00+00:00'"
    ).fetchone()[0] == 1


def test_observation_and_mitigation_follow_business_time(service: TempleSafetyService):
    v1 = publish(service, V1_RULES, datetime(2026, 9, 1, tzinfo=UTC))
    v2 = publish(service, V2_RULES, datetime(2026, 10, 1, tzinfo=UTC))
    add_authorization(service, "order-timeline-0001")
    # 9-30 的观测（切换前写入）锚定旧版。
    before = service.ingest_observation(observation_payload(
        key="observation-before-boundary",
        observed_at=datetime(2026, 9, 30, 23, 0, tzinfo=UTC),
    ))
    assert before["safety_incident_id"] is not None
    assert before["quality"]["safety_policy_version_id"] == v1["id"]
    incident_before = service.connection.execute(
        "SELECT safety_policy_version_id FROM safety_incidents WHERE id=?",
        (before["safety_incident_id"],),
    ).fetchone()
    assert incident_before["safety_policy_version_id"] == v1["id"]
    # 时钟跨过 10-01：写入路径自动闭合旧版边界，10-01 的观测锚定新版。
    service.clock.current = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)
    # 迟到送达、业务时间仍在 9-30 的观测严格按业务时间锚定旧版。
    late = service.ingest_observation(observation_payload(
        key="observation-late-arrival",
        observed_at=datetime(2026, 9, 30, 22, 0, tzinfo=UTC),
        steward="steward-ceremony-0003",
    ))
    assert late["quality"]["safety_policy_version_id"] == v1["id"]
    add_authorization(service, "order-timeline-0003", steward="steward-ceremony-0003")
    late_session = service.start_mitigation(late["safety_incident_id"], "supervisor-a")
    assert late_session["safety_policy_version_id"] == v1["id"]
    after = service.ingest_observation(observation_payload(
        key="observation-after-boundary",
        observed_at=datetime(2026, 10, 1, 0, 20, tzinfo=UTC),
        steward="steward-ceremony-0002",
    ))
    assert after["quality"]["safety_policy_version_id"] == v2["id"]
    add_authorization(service, "order-timeline-0002", steward="steward-ceremony-0002")
    # 旧隐患的通风处置参数仍引用旧版（180 秒），新隐患引用新版（240 秒）。
    old_session = service.start_mitigation(before["safety_incident_id"], "supervisor-a")
    new_session = service.start_mitigation(after["safety_incident_id"], "supervisor-a")
    assert old_session["safety_policy_version_id"] == v1["id"]
    assert new_session["safety_policy_version_id"] == v2["id"]
    old_expires = datetime.fromisoformat(old_session["expires_at"])
    new_expires = datetime.fromisoformat(new_session["expires_at"])
    started = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)
    assert old_expires == started + timedelta(seconds=180)
    assert new_expires == started + timedelta(seconds=240)
    assert [event["detail"].get("safety_policy_version") for event in old_session["events"]] == [v1["version_no"]]
    # 旧版此时已退役，但处置事件与历史查询仍能审计到它。
    assert service.connection.execute("SELECT state FROM safety_policy_versions WHERE id=?", (v1["id"],)).fetchone()["state"] == "retired"
    assert service.effective_safety_policy("fayun-temple", "2026-09-30T23:00:00Z")["id"] == v1["id"]


def test_duplicate_observation_keeps_original_anchor(service: TempleSafetyService):
    publish(service, V1_RULES, datetime(2026, 9, 1, tzinfo=UTC))
    publish(service, V2_RULES, datetime(2026, 10, 1, tzinfo=UTC))
    payload = observation_payload(key="observation-duplicate", observed_at=datetime(2026, 9, 30, 23, 0, tzinfo=UTC))
    first = service.ingest_observation(payload)
    service.clock.current = datetime(2026, 10, 2, tzinfo=UTC)
    repeated = service.ingest_observation(payload)
    assert repeated["observation_id"] == first["observation_id"]
    assert repeated["safety_incident_id"] == first["safety_incident_id"]
    anchor = service.connection.execute(
        "SELECT safety_policy_version_id FROM safety_incidents WHERE id=?",
        (first["safety_incident_id"],),
    ).fetchone()
    assert anchor["safety_policy_version_id"] == first["quality"]["safety_policy_version_id"]


def test_policy_history_and_effective_api(client):
    def prepare():
        client.post("/api/temple/temples", json=TEMPLE_PAYLOAD)
        client.post(
            "/api/temple/temples/fayun-temple/halls",
            json={"code": "main-hall", "name": "大雄宝殿", "visit_order": 1, "expected_visit_seconds": 900, "ventilation_capacity": 1200},
        )
        client.post("/api/temple/incense_profiles", json=PROFILE_PAYLOAD)
        v1 = client.post("/api/temple/temples/fayun-temple/policies", json={"rules": V1_RULES, "actor": "a"}).json()
        client.post(f"/api/temple/policies/{v1['id']}/publish", json={"actor": "a", "effective_from": "2026-09-01T00:00:00Z"})
        v2 = client.post("/api/temple/temples/fayun-temple/policies", json={"rules": V2_RULES, "actor": "b"}).json()
        client.post(f"/api/temple/policies/{v2['id']}/publish", json={"actor": "b", "effective_from": "2026-10-01T00:00:00Z"})
        return v1, v2

    v1, v2 = prepare()
    history = client.get("/api/temple/temples/fayun-temple/policies")
    assert history.status_code == 200
    assert {item["version_no"] for item in history.json()["items"]} == {v1["version_no"], v2["version_no"]}
    before = client.get("/api/temple/policies/effective", params={"temple_code": "fayun-temple", "at": "2026-09-30T12:00:00Z"})
    assert before.status_code == 200 and before.json()["id"] == v1["id"]
    after = client.get("/api/temple/policies/effective", params={"temple_code": "fayun-temple", "at": "2026-10-01T00:00:00Z"})
    assert after.status_code == 200 and after.json()["id"] == v2["id"]
    conflict = client.post(
        f"/api/temple/policies/{v2['id']}/publish",
        json={"actor": "b", "effective_from": "2026-10-02T00:00:00Z"},
    )
    assert conflict.status_code == 409


def test_migration_repairs_early_retirement_gap(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "CREATE TABLE safety_policy_versions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,temple_id INTEGER NOT NULL,version_no INTEGER NOT NULL,"
        "state TEXT NOT NULL DEFAULT 'draft',rules_json TEXT NOT NULL,rules_digest TEXT NOT NULL,"
        "created_by TEXT NOT NULL,published_by TEXT,effective_from TEXT,retired_at TEXT,"
        "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO safety_policy_versions(temple_id,version_no,state,rules_json,rules_digest,created_by,"
        "published_by,effective_from,retired_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            (7, 1, "retired", "{}", "d1", "legacy", "legacy", "2026-09-01T00:00:00+00:00", "2026-09-20T00:00:00+00:00", "t", "t"),
            (7, 2, "published", "{}", "d2", "legacy", "legacy", "2026-10-01T00:00:00+00:00", None, "t", "t"),
        ],
    )
    connection.commit()
    connection.close()

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    ensure_temple_schema(connection)
    repository = TempleRepository(connection)
    # 旧版恢复为已发布并补上结束边界，空档消失。
    restored = repository.effective_safety_policy(7, "2026-09-25T00:00:00+00:00")
    assert restored is not None and restored["version_no"] == 1
    current = repository.effective_safety_policy(7, "2026-10-02T00:00:00+00:00")
    assert current is not None and current["version_no"] == 2
    boundary = connection.execute("SELECT effective_to,retired_at FROM safety_policy_versions WHERE version_no=1").fetchone()
    assert boundary["effective_to"] == "2026-10-01T00:00:00+00:00"
    assert boundary["retired_at"] == "2026-09-20T00:00:00+00:00"
    connection.close()
