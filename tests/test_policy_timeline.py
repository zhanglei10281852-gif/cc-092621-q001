from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock, to_storage
from app.core.errors import ConflictError
from app.database import close_connection, get_connection
from app.temple.rules import DEFAULT_RULES
from app.temple.service import TempleSafetyService

T0 = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
SWITCH = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)


def changed_rules(duration: int) -> dict:
    return {**DEFAULT_RULES, "allocation": {**DEFAULT_RULES["allocation"], "duration_seconds": duration}}


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMPLE_DATABASE_PATH", str(tmp_path / "policy.db"))
    close_connection()
    clock = FrozenClock(T0)
    svc = TempleSafetyService(get_connection(), clock)
    svc.create_temple(
        {
            "code": "fayu-temple",
            "name": "法源寺",
            "temple_type": "heritage",
            "timezone": "Asia/Shanghai",
            "max_concurrent_mitigation_sessions": 10,
            "ventilation_capacity": 3000,
        }
    )
    svc.add_hall(
        "fayu-temple",
        {"code": "main-hall", "name": "大雄宝殿", "visit_order": 1, "expected_visit_seconds": 900, "ventilation_capacity": 1200},
    )
    svc.create_incense_profile(
        {
            "incense_code": "festival-incense",
            "name": "节庆香火",
            "activity_type": "festival",
            "pm25_target": 100,
            "co_target": 0.01,
            "min_supply_airflow": 8,
            "min_exhaust_airflow": 4,
            "default_risk_priority": 70,
        }
    )
    v1 = svc.create_safety_policy("fayu-temple", DEFAULT_RULES, "supervisor-a")
    v1 = svc.publish_safety_policy(v1["id"], "supervisor-a", to_storage(T0))
    v2 = svc.create_safety_policy("fayu-temple", changed_rules(240), "supervisor-b")
    v2 = svc.publish_safety_policy(v2["id"], "supervisor-b", to_storage(SWITCH))
    svc.add_authorization(
        {
            "steward_hash": "steward-000000000001",
            "temple_code": "fayu-temple",
            "authorization_code": "festival-duty",
            "valid_from": to_storage(T0),
            "valid_until": to_storage(LATER),
            "source_approval_id": "order-policy-001",
        }
    )
    return svc


def observation(service: TempleSafetyService, key: str, observed_at: datetime) -> dict:
    return service.ingest_observation(
        {
            "observation_key": key,
            "temple_code": "fayu-temple",
            "hall_code": "main-hall",
            "incense_code": "festival-incense",
            "steward_hash": "steward-000000000001",
            "sensor_class": "ceiling-sensor",
            "visitor_density": 300,
            "pm25_ugm3": 350,
            "co_ppm": 0.08,
            "supply_airflow": 1.5,
            "exhaust_airflow": 0.5,
            "observed_at": to_storage(observed_at),
        }
    )


def states(service: TempleSafetyService) -> dict[int, str]:
    rows = service.repository.policies(1)
    return {row["version_no"]: row["state"] for row in rows}


def test_future_publication_only_registers_appointment(service):
    # 九月三十日：十月一日生效的新版只登记预约，此刻的有效版本不变。
    assert states(service) == {1: "published", 2: "scheduled"}
    active = service.repository.active_safety_policy(1)
    assert active["version_no"] == 1
    assert active["retired_at"] is None


def test_effective_query_is_unique_and_continuous_across_switch(service):
    repo = service.repository
    # 切换前任意时刻都命中 v1，空档期不会没有可用策略。
    for moment in ("2026-09-30T00:00:00+00:00", "2026-09-30T12:00:00+00:00", "2026-09-30T23:59:59+00:00"):
        assert repo.safety_policy_as_of(1, moment)["version_no"] == 1

    # 推进可控时钟跨过约定时点：v2 激活，v1 同时留下结束边界。
    service.clock.current = SWITCH
    result = service.activate_due_safety_policies()
    assert result["activated"] == [2]
    assert states(service) == {1: "retired", 2: "published"}
    rows = {row["version_no"]: row for row in repo.policies(1)}
    assert rows[1]["retired_at"] == to_storage(SWITCH)
    assert rows[1]["retired_reason"] == "superseded"
    assert rows[2]["retired_at"] is None

    # 切换时点本身归新版；切换前后唯一且连续。
    assert repo.safety_policy_as_of(1, "2026-09-30T23:59:59+00:00")["version_no"] == 1
    assert repo.safety_policy_as_of(1, to_storage(SWITCH))["version_no"] == 2
    assert repo.safety_policy_as_of(1, "2026-10-01T12:00:00+00:00")["version_no"] == 2
    # 旧版的结束边界正好是新版的开始边界：无重叠、无空档。
    assert rows[1]["effective_from"] <= to_storage(SWITCH) == rows[1]["retired_at"] == rows[2]["effective_from"]


def test_observations_and_mitigations_follow_business_time(service):
    # 九月三十日 12:00 的观测按 v1 评判。
    old = observation(service, "observation-v1-001", datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
    assert old["safety_policy_version_no"] == 1
    session_v1 = service.start_mitigation(old["safety_incident_id"], "supervisor-a")
    assert session_v1["safety_policy_version_id"] == old["safety_policy_version_id"]

    # 到达十月一日：新产生的隐患切换到 v2。
    service.clock.current = SWITCH
    new = observation(service, "observation-v2-001", datetime(2026, 10, 1, 0, 0, 30, tzinfo=UTC))
    assert new["safety_policy_version_no"] == 2
    session_v2 = service.start_mitigation(new["safety_incident_id"], "supervisor-a")
    assert session_v2["safety_policy_version_id"] == new["safety_policy_version_id"]
    # v2 的处置参数来自新版（duration_seconds=240），处置按发起时刻（时钟）起算。
    assert session_v2["expires_at"] == "2026-10-01T00:04:00+00:00"
    assert session_v1["id"] != session_v2["id"]


def test_duplicate_publication_is_idempotent(service):
    v2 = service.repository.policies(1)[0]
    # 完全重复的提交返回同一预约，不改时间线。
    again = service.publish_safety_policy(v2["id"], "supervisor-b", to_storage(SWITCH))
    assert again["id"] == v2["id"]
    assert again["effective_from"] == to_storage(SWITCH)
    assert states(service) == {1: "published", 2: "scheduled"}
    # 同一版本想改约到别的时点：拒绝。
    with pytest.raises(ConflictError) as exc:
        service.publish_safety_policy(v2["id"], "supervisor-b", "2026-10-02T00:00:00+00:00")
    assert exc.value.status_code == 409


def test_two_supervisors_cannot_schedule_same_switch_instant(service):
    rival = service.create_safety_policy("fayu-temple", changed_rules(300), "supervisor-c")
    # 两名主管把不同版本约到同一时点：第二个必须失败，时间线保持唯一。
    with pytest.raises(ConflictError) as exc:
        service.publish_safety_policy(rival["id"], "supervisor-c", to_storage(SWITCH))
    assert exc.value.status_code == 409
    # 约到不同时点允许，形成预约链。
    chained = service.publish_safety_policy(rival["id"], "supervisor-c", "2026-10-02T00:00:00+00:00")
    assert chained["state"] == "scheduled"
    assert states(service) == {1: "published", 2: "scheduled", 3: "scheduled"}
    # 一次性跨过两个预约时点：链按业务时间顺序整体切换，不产生重叠版本。
    service.clock.current = LATER
    service.activate_due_safety_policies()
    final = states(service)
    assert final == {1: "retired", 2: "retired", 3: "published"}
    timeline = sorted(service.repository.policies(1), key=lambda row: row["version_no"])
    assert timeline[0]["retired_at"] == timeline[1]["effective_from"]
    assert timeline[1]["retired_at"] == timeline[2]["effective_from"]


def test_two_supervisors_racing_at_same_instant_only_one_wins(tmp_path, monkeypatch):
    db_path = tmp_path / "race.db"
    monkeypatch.setenv("TEMPLE_DATABASE_PATH", str(db_path))
    close_connection()
    organizer = TempleSafetyService(get_connection(), FrozenClock(T0))
    organizer.create_temple(
        {
            "code": "fayu-temple",
            "name": "法源寺",
            "temple_type": "heritage",
            "timezone": "Asia/Shanghai",
            "max_concurrent_mitigation_sessions": 10,
            "ventilation_capacity": 3000,
        }
    )
    v1 = organizer.create_safety_policy("fayu-temple", DEFAULT_RULES, "supervisor-a")
    organizer.publish_safety_policy(v1["id"], "supervisor-a", to_storage(T0))
    drafts = [
        organizer.create_safety_policy("fayu-temple", changed_rules(240), "supervisor-b")["id"],
        organizer.create_safety_policy("fayu-temple", changed_rules(300), "supervisor-c")["id"],
    ]
    outcomes: list[BaseException | None] = [None, None]
    barrier = threading.Barrier(2)

    def publish(index: int) -> None:
        close_connection()
        svc = TempleSafetyService(get_connection(), FrozenClock(T0))
        barrier.wait()
        try:
            svc.publish_safety_policy(drafts[index], f"supervisor-{chr(98 + index)}", to_storage(SWITCH))
        except BaseException as exc:  # noqa: BLE001 - 记录到结果列表
            outcomes[index] = exc

    threads = [threading.Thread(target=publish, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    conflicts = [exc for exc in outcomes if isinstance(exc, ConflictError)]
    assert len(conflicts) == 1, outcomes
    close_connection()
    rows = TempleSafetyService(get_connection(), FrozenClock(T0)).repository.policies(1)
    scheduled = [row for row in rows if row["state"] == "scheduled"]
    assert len(scheduled) == 1
    # 竞标失败的版本保持 draft，时间线上仍只有一个现行版本和一个预约版本。
    assert [row["state"] for row in rows if row["state"] != "draft"] == ["scheduled", "published"]


def test_no_policy_gap_inside_the_gap_window(service):
    # 题目里的“空档”现场：新版已预约但未生效时，观测仍能拿到处置参数。
    service.clock.current = datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
    result = observation(service, "observation-gap-001", datetime(2026, 9, 30, 23, 0, tzinfo=UTC))
    assert result["safety_policy_version_no"] == 1
    assert result["safety_incident_id"] is not None
    started = service.start_mitigation(result["safety_incident_id"], "supervisor-a")
    assert started["status"] == "active"
