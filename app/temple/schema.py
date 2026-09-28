from __future__ import annotations

import sqlite3

from app.core.clock import to_storage, utc_now

SAFETY_POLICY_TABLE_DDL = r'''
CREATE TABLE IF NOT EXISTS safety_policy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','scheduled','published','retired')),
    rules_json TEXT NOT NULL,
    rules_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    published_by TEXT,
    effective_from TEXT,
    retired_at TEXT,
    retired_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(temple_id, version_no),
    UNIQUE(temple_id, rules_digest)
);
'''

SAFETY_POLICY_INDEX_DDL = r'''
-- 同一寺院至多一个当前有效版本；同一预约时点至多一个预约版本（不同时点可串成
-- 预约链）；退役版本永久保留作为可审计的结束边界。draft 版本被 WHERE 排除。
CREATE UNIQUE INDEX IF NOT EXISTS idx_safety_policy_active ON safety_policy_versions(temple_id) WHERE state='published';
CREATE UNIQUE INDEX IF NOT EXISTS idx_safety_policy_scheduled ON safety_policy_versions(temple_id,effective_from) WHERE state='scheduled';
CREATE INDEX IF NOT EXISTS idx_safety_policy_effective ON safety_policy_versions(temple_id,state,effective_from);
'''

TEMPLE_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS temple_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    temple_type TEXT NOT NULL CHECK(temple_type IN ('heritage','urban','mountain','community')),
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','retired')),
    max_concurrent_mitigation_sessions INTEGER NOT NULL CHECK(max_concurrent_mitigation_sessions > 0),
    ventilation_capacity INTEGER NOT NULL CHECK(ventilation_capacity > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worship_halls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    visit_order INTEGER NOT NULL CHECK(visit_order >= 0),
    expected_visit_seconds INTEGER NOT NULL CHECK(expected_visit_seconds > 0),
    ventilation_capacity INTEGER NOT NULL CHECK(ventilation_capacity > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closure','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(temple_id, code),
    UNIQUE(temple_id, visit_order)
);
CREATE TABLE IF NOT EXISTS incense_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incense_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    activity_type TEXT NOT NULL CHECK(activity_type IN ('daily','festival','ceremony','memorial','tour')),
    pm25_target INTEGER NOT NULL CHECK(pm25_target > 0),
    co_target REAL NOT NULL CHECK(co_target >= 0 AND co_target <= 1),
    min_supply_airflow REAL NOT NULL CHECK(min_supply_airflow >= 0),
    min_exhaust_airflow REAL NOT NULL CHECK(min_exhaust_airflow >= 0),
    default_risk_priority INTEGER NOT NULL CHECK(default_risk_priority BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','retired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
''' + SAFETY_POLICY_TABLE_DDL + r'''
CREATE TABLE IF NOT EXISTS incense_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_key TEXT NOT NULL UNIQUE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    incense_profile_id INTEGER NOT NULL REFERENCES incense_profiles(id),
    steward_hash TEXT NOT NULL,
    sensor_class TEXT NOT NULL,
    visitor_density REAL NOT NULL CHECK(visitor_density >= 0),
    pm25_ugm3 REAL NOT NULL CHECK(pm25_ugm3 >= 0),
    co_ppm REAL NOT NULL CHECK(co_ppm >= 0 AND co_ppm <= 1),
    supply_airflow REAL NOT NULL CHECK(supply_airflow >= 0),
    exhaust_airflow REAL NOT NULL CHECK(exhaust_airflow >= 0),
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    safety_policy_version_id INTEGER REFERENCES safety_policy_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_observations_scene_time ON incense_observations(temple_id,observed_at);
CREATE TABLE IF NOT EXISTS safety_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL UNIQUE REFERENCES incense_observations(id) ON DELETE CASCADE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    incense_profile_id INTEGER NOT NULL REFERENCES incense_profiles(id),
    severity TEXT NOT NULL CHECK(severity IN ('minor','major','critical')),
    reasons_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','mitigating','resolved','expired')),
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_safety_incidents_open ON safety_incidents(state,severity,opened_at);
CREATE TABLE IF NOT EXISTS mitigation_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    safety_incident_id INTEGER NOT NULL REFERENCES safety_incidents(id),
    steward_hash TEXT NOT NULL,
    incense_profile_id INTEGER NOT NULL REFERENCES incense_profiles(id),
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    safety_policy_version_id INTEGER NOT NULL REFERENCES safety_policy_versions(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','cancelled','expired')),
    allocated_supply_airflow REAL NOT NULL CHECK(allocated_supply_airflow >= 0),
    allocated_exhaust_airflow REAL NOT NULL CHECK(allocated_exhaust_airflow >= 0),
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(safety_incident_id)
);
CREATE INDEX IF NOT EXISTS idx_mitigation_sessions_ventilation ON mitigation_sessions(temple_id,hall_id,status,expires_at);
CREATE TABLE IF NOT EXISTS ventilation_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mitigation_session_id INTEGER NOT NULL REFERENCES mitigation_sessions(id) ON DELETE CASCADE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    supply_airflow REAL NOT NULL,
    exhaust_airflow REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    held_at TEXT NOT NULL,
    released_at TEXT,
    UNIQUE(mitigation_session_id)
);
CREATE TABLE IF NOT EXISTS mitigation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mitigation_session_id INTEGER NOT NULL REFERENCES mitigation_sessions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mitigation_events ON mitigation_events(mitigation_session_id,id);
CREATE TABLE IF NOT EXISTS steward_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    steward_hash TEXT NOT NULL,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    authorization_code TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','expired','cancelled')),
    source_approval_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authorizations_lookup ON steward_authorizations(steward_hash,temple_id,state,valid_from,valid_until);
CREATE TABLE IF NOT EXISTS restoration_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK(strategy IN ('phased','halls','scheduled')),
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','scheduled','running','paused','completed','cancelled')),
    target_percentage INTEGER NOT NULL DEFAULT 100 CHECK(target_percentage BETWEEN 1 AND 100),
    safety_policy_version_id INTEGER NOT NULL REFERENCES safety_policy_versions(id),
    starts_at TEXT,
    ends_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS restoration_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restoration_campaign_id INTEGER NOT NULL REFERENCES restoration_campaigns(id) ON DELETE CASCADE,
    hall_id INTEGER REFERENCES worship_halls(id),
    cohort_key TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','active','paused','completed','failed')),
    activated_at TEXT,
    completed_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(restoration_campaign_id,hall_id,cohort_key)
);
CREATE INDEX IF NOT EXISTS idx_restoration_targets_state ON restoration_targets(restoration_campaign_id,state,id);
CREATE TABLE IF NOT EXISTS hall_closure_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    code TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'scheduled' CHECK(state IN ('scheduled','active','completed','cancelled')),
    drain_mode TEXT NOT NULL DEFAULT 'finish_active' CHECK(drain_mode IN ('finish_active','cancel_active','block_new')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_closure_active ON hall_closure_windows(temple_id,hall_id,state,starts_at,ends_at);
CREATE TABLE IF NOT EXISTS restoration_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    resource_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_restoration_events_resource ON restoration_events(resource_type,resource_id,id);
'''


def ensure_temple_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(TEMPLE_SCHEMA)
    _ensure_safety_policy_table(connection)
    connection.executescript(SAFETY_POLICY_INDEX_DDL)
    _ensure_observation_policy_column(connection)


def _ensure_observation_policy_column(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(incense_observations)")}
    if "safety_policy_version_id" not in columns:
        connection.execute(
            "ALTER TABLE incense_observations ADD COLUMN safety_policy_version_id INTEGER REFERENCES safety_policy_versions(id)"
        )


def _ensure_safety_policy_table(connection: sqlite3.Connection) -> None:
    connection.executescript(SAFETY_POLICY_TABLE_DDL)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(safety_policy_versions)")}
    if "retired_reason" in columns:
        return
    # 旧版本表缺少 scheduled 状态（受 CHECK 约束限制）与 retired_reason 列，
    # SQLite 无法直接修改约束，按外键依赖顺序重建。
    cutover = to_storage(utc_now())
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.executescript(
            "ALTER TABLE safety_policy_versions RENAME TO safety_policy_versions_legacy;\n"
            + SAFETY_POLICY_TABLE_DDL
        )
        # 旧实现没有 scheduled：未来才生效却已 published 的行要还原为预约；
        # 已退役行补 retired_reason='superseded'。
        connection.execute(
            """
            INSERT INTO safety_policy_versions(
                id,temple_id,version_no,state,rules_json,rules_digest,created_by,published_by,
                effective_from,retired_at,retired_reason,created_at,updated_at
            )
            SELECT id,temple_id,version_no,
                   CASE WHEN state='published' AND effective_from>:cutover THEN 'scheduled' ELSE state END,
                   rules_json,rules_digest,created_by,published_by,
                   effective_from,retired_at,
                   CASE WHEN state='retired' THEN 'superseded' ELSE '' END,
                   created_at,updated_at
            FROM safety_policy_versions_legacy
            """,
            {"cutover": cutover},
        )
        connection.execute("DROP TABLE safety_policy_versions_legacy")
        # 旧数据若在同一预约时点残留多个 published 行，只保留 id 最大的一个为
        # scheduled，其余退回 draft，使 (temple_id,effective_from) 偏序唯一索引
        # 能够建立。
        connection.execute(
            """
            UPDATE safety_policy_versions SET state='draft', published_by=NULL, effective_from=NULL
            WHERE state='scheduled' AND id NOT IN (
                SELECT MAX(id) FROM safety_policy_versions WHERE state='scheduled'
                GROUP BY temple_id,effective_from
            )
            """
        )
        # 旧数据若残留多个已到点的 published 行（旧表没有唯一约束），每寺院只保留
        # 生效时点最新的一个，其余转为退役，使偏序唯一索引能够建立。
        connection.execute(
            """
            UPDATE safety_policy_versions SET state='retired', retired_reason='superseded'
            WHERE state='published' AND effective_from<=:cutover AND id NOT IN (
                SELECT winner_id FROM (
                    SELECT (SELECT p2.id FROM safety_policy_versions p2
                            WHERE p2.temple_id=p.temple_id AND p2.state='published'
                            ORDER BY p2.effective_from DESC,p2.id DESC LIMIT 1) AS winner_id
                    FROM safety_policy_versions p
                    WHERE p.state='published' AND p.effective_from<=:cutover
                    GROUP BY p.temple_id
                )
            )
            """,
            {"cutover": cutover},
        )
        # 旧实现把 retired_at 记为发布操作时间，可能早于后继版本的生效时点而留下
        # 空档；统一用时间线上下一版本的生效时点重写结束边界。
        connection.execute(
            "UPDATE safety_policy_versions SET retired_at=("
            "SELECT MIN(n.effective_from) FROM safety_policy_versions n "
            "WHERE n.temple_id=safety_policy_versions.temple_id "
            "AND n.effective_from>safety_policy_versions.effective_from) "
            "WHERE state='retired'"
        )
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
