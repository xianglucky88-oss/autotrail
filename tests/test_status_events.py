"""挂牌事件状态机：确定性、乱序、冲突、恢复、崩溃恢复与错误分类。"""
import sqlite3

import pytest

from src.autotrail import keyer, pipeline, statusline, store


def ev(status, ts, collected="2026-01-01T00:00:00Z", vin="VIN00000000000001", **extra):
    record = {
        "vin": vin,
        "status": status,
        "ts": ts,
        "collected_at": collected,
    }
    record.update(extra)
    return record


def run_order(records):
    """返回 (当前状态, 转移序列, 事件键序列)，用于输入顺序无关性断言。"""
    proj = statusline.projection(records)
    transitions = [(t["status"], t["before"], t["after"]) for t in proj["transitions"]]
    return proj["status"], transitions, [e["event_key"] for e in proj["events"]]


# ---------------------------------------------------------------------------
# 迟到/回退：曾成交又收到更早的在售
# ---------------------------------------------------------------------------
def test_late_active_after_sold_stays_sold_for_any_input_order():
    base = ev("active", "2026-01-01T00:00:00Z")
    sold = ev("sold", "2026-01-05T00:00:00Z")
    late_active = ev("active", "2026-01-03T00:00:00Z", collected="2026-02-01T00:00:00Z")

    expected_keys = run_order([base, sold, late_active])[2]
    for order in ([base, sold, late_active], [late_active, sold, base], [sold, late_active, base]):
        status, transitions, keys = run_order(order)
        assert status == "sold", order
        assert transitions == [
            ("active", None, "active"),
            ("active", "active", "active"),
            ("sold", "active", "sold"),
        ], order
        assert keys == expected_keys, order

    # 明确的单点答案入口
    assert statusline.resolve_late_active_after_sold(
        [late_active, sold, base]
    ) == "sold"

    # 只有当在售事件时间晚于成交，才是真正的重新挂牌
    re_listed = ev("active", "2026-01-06T00:00:00Z")
    assert statusline.resolve_late_active_after_sold([sold, base, re_listed]) == "active"


def test_reordered_input_produces_identical_event_keys():
    records = [
        ev("active", "2026-01-01T00:00:00Z"),
        ev("reserved", "2026-01-02T00:00:00Z"),
        ev("sold", "2026-01-03T00:00:00Z"),
    ]
    expected = sorted(k["event_key"] for k in map(statusline.normalize_event, records))
    orders = (records, records[::-1], [records[1], records[0], records[2]])
    for order in orders:
        _, _, keys = run_order(order)
        assert sorted(keys) == expected  # 键集合与输入顺序无关
    # 历史应用顺序（事件时间序）也与输入顺序无关
    assert run_order(records)[2] == run_order(records[::-1])[2]


# ---------------------------------------------------------------------------
# 重复事件
# ---------------------------------------------------------------------------
def test_duplicate_events_are_deduped():
    record = ev("active", "2026-01-01T00:00:00Z")
    status, transitions, keys = run_order([record, dict(record), record])
    assert status == "active"
    assert len(transitions) == 1
    assert len(set(keys)) == 1


# ---------------------------------------------------------------------------
# 同一时刻冲突
# ---------------------------------------------------------------------------
def test_simultaneous_conflict_uses_collected_at_then_precedence():
    # 同一事件时刻：更晚的采集观测胜出（它看到的是同一时刻的更新信息）
    sold = ev("sold", "2026-01-05T00:00:00Z", collected="2026-01-05T01:00:00Z")
    active = ev("active", "2026-01-05T00:00:00Z", collected="2026-01-05T02:00:00Z")
    proj = statusline.projection([sold, active])
    assert proj["status"] == "active"
    decisions = {e["status"]: e["adjudicated_by"] for e in proj["events"]}
    assert decisions["active"] == "collected_at"

    # 采集时间也相同 → 状态优先级裁决（sold 最高）
    sold_same = ev("sold", "2026-01-05T00:00:00Z", collected="2026-01-05T01:00:00Z")
    active_same = ev("active", "2026-01-05T00:00:00Z", collected="2026-01-05T01:00:00Z")
    proj2 = statusline.projection([active_same, sold_same])
    assert proj2["status"] == "sold"
    assert {e["adjudicated_by"] for e in proj2["events"]} == {"status_precedence"}
    assert {e["conflict_group"] for e in proj2["events"]} == {
        "vin:VIN00000000000001:2026-01-05T00:00:00.000000Z"
    }


def test_simultaneous_conflict_outcome_independent_of_input_order():
    records = [
        ev("active", "2026-01-05T00:00:00Z", collected="2026-01-05T02:00:00Z"),
        ev("sold", "2026-01-05T00:00:00Z", collected="2026-01-05T01:00:00Z"),
        ev("withdrawn", "2026-01-05T00:00:00Z", collected="2026-01-05T03:00:00Z"),
    ]
    results = {run_order(order)[0] for order in (records, records[::-1], [records[1], records[2], records[0]])}
    assert results == {"withdrawn"}


# ---------------------------------------------------------------------------
# 撤销与恢复位置
# ---------------------------------------------------------------------------
def test_withdraw_then_relist_restores_previous_position():
    records = [
        ev("active", "2026-01-01T00:00:00Z"),
        ev("reserved", "2026-01-02T00:00:00Z"),
        ev("withdrawn", "2026-01-03T00:00:00Z"),
        ev("relisted", "2026-01-04T00:00:00Z"),
    ]
    proj = statusline.projection(records)
    assert proj["status"] == "reserved"  # 恢复到撤销前的预订
    assert proj["restore_position"] == "reserved"
    last = proj["transitions"][-1]
    assert (last["before"], last["after"]) == ("withdrawn", "reserved")

    # 任意顺序仍一致
    assert statusline.projection(records[::-1])["status"] == "reserved"


def test_relist_without_withdraw_falls_back_to_active():
    proj = statusline.projection(
        [ev("sold", "2026-01-01T00:00:00Z"), ev("relisted", "2026-01-02T00:00:00Z")]
    )
    assert proj["status"] == "active"
    assert proj["restore_position"] == "active"


# ---------------------------------------------------------------------------
# 下架后重现
# ---------------------------------------------------------------------------
def test_delist_then_reappear_in_any_order():
    records = [
        ev("active", "2026-01-01T00:00:00Z"),
        ev("delisted", "2026-01-02T00:00:00Z"),
        ev("active", "2026-01-03T00:00:00Z"),
    ]
    expected = [("active", None, "active"), ("delisted", "active", "delisted"), ("active", "delisted", "active")]
    for order in (records, records[::-1], [records[1], records[0], records[2]]):
        status, transitions, _ = run_order(order)
        assert status == "active"
        assert transitions == expected


# ---------------------------------------------------------------------------
# keyer 稳定键
# ---------------------------------------------------------------------------
def test_keyer_identity_priority():
    assert keyer.vehicle_key_of({"vin": " abc123 ", "id": "x"}) == "vin:ABC123"
    assert keyer.vehicle_key_of({"src": "Alpha", "id": "L1"}) == "src:alpha:L1"
    with pytest.raises(keyer.MissingIdentity):
        keyer.vehicle_key_of({"name": "no identity"})


def test_event_key_is_stable_and_field_sensitive():
    a = keyer.event_key_of(ev("active", "2026-01-01T00:00:00Z"))
    b = keyer.event_key_of(ev("active", "2026-01-01T00:00:00Z"))
    c = keyer.event_key_of(ev("sold", "2026-01-01T00:00:00Z"))
    assert a == b and a != c


# ---------------------------------------------------------------------------
# store：历史/投影/提交确认/恢复/错误台账
# ---------------------------------------------------------------------------
@pytest.fixture()
def con(tmp_path):
    connection = store.connect(str(tmp_path / "events.db"))
    yield connection
    connection.close()


def test_store_appends_history_and_updates_projection(con):
    receipt = store.ingest(
        con,
        ev("active", "2026-01-01T00:00:00Z"),
    )
    assert receipt["ok"] and receipt["inserted"] and receipt["committed"]
    assert store.event_count(con) == 1
    assert store.get_projection(con, "vin:VIN00000000000001")["status"] == "active"

    duplicate = store.ingest(con, ev("active", "2026-01-01T00:00:00Z"))
    assert duplicate["ok"] and not duplicate["inserted"]
    assert duplicate["outcome"] == "duplicate"
    assert store.event_count(con) == 1


def test_history_persists_adjudication_and_restore_position(con):
    records = [
        ev("active", "2026-01-01T00:00:00Z"),
        ev("reserved", "2026-01-02T00:00:00Z"),
        ev("withdrawn", "2026-01-03T00:00:00Z"),
        ev("relisted", "2026-01-04T00:00:00Z"),
        ev("active", "2026-01-05T00:00:00Z", collected="2026-01-05T02:00:00Z"),
        ev("sold", "2026-01-05T00:00:00Z", collected="2026-01-05T03:00:00Z"),
    ]
    store.ingest_many(con, records)

    rows = con.execute(
        "SELECT status,adjudicated_by,conflict_group,restore_position,ordinal "
        "FROM events ORDER BY ordinal"
    ).fetchall()
    by_status = {row[0]: row for row in rows}
    assert by_status["withdrawn"][3] == "reserved"  # 撤销保存的恢复位置
    assert by_status["relisted"][3] == "reserved"
    assert by_status["sold"][1] == "collected_at"
    assert by_status["sold"][2] is not None
    assert [row[4] for row in rows] == list(range(1, len(records) + 1))

    projection = store.get_projection(con, "vin:VIN00000000000001")
    assert projection["status"] == "sold"
    assert projection["restore_position"] == "reserved"


def test_interrupted_projection_recovers_from_history(con):
    store.ingest_many(
        con,
        [
            ev("active", "2026-01-01T00:00:00Z"),
            ev("sold", "2026-01-02T00:00:00Z"),
        ],
    )
    # 模拟“历史已写、投影未更新”
    con.execute(
        "UPDATE projections SET status='active',history_len=1 "
        "WHERE vehicle_key='vin:VIN00000000000001'"
    )
    con.commit()
    assert store.get_projection(con, "vin:VIN00000000000001")["status"] == "active"

    result = store.recover(con)
    assert result["repaired"] == ["vin:VIN00000000000001"]
    stored = store.get_projection(con, "vin:VIN00000000000001")
    replayed = store.replay_projection(con, "vin:VIN00000000000001")
    assert stored["status"] == replayed["status"] == "sold"
    assert stored["history_len"] == replayed["history_len"] == 2
    assert store.recover(con)["count"] == 0  # 已一致，再跑无修复


def test_projection_missing_after_crash_is_rebuilt(con):
    event = statusline.normalize_event(ev("active", "2026-01-01T00:00:00Z"))
    store.append_event(con, event)
    con.commit()  # 只有历史，没有投影
    assert store.get_projection(con, "vin:VIN00000000000001") is None

    assert store.recover(con)["count"] == 1
    assert store.get_projection(con, "vin:VIN00000000000001")["status"] == "active"


def test_crash_between_history_and_projection_survives_reconnect(tmp_path):
    path = str(tmp_path / "crash.db")
    first = store.connect(path)
    # 中断点：事件历史已提交，投影更新尚未发生
    for record in (
        ev("active", "2026-01-01T00:00:00Z"),
        ev("reserved", "2026-01-02T00:00:00Z"),
    ):
        store.append_event(first, statusline.normalize_event(record))
    first.commit()
    first.close()  # 未调用 refresh_projection，模拟进程中断

    second = store.connect(path)
    try:
        assert store.get_projection(second, "vin:VIN00000000000001") is None
        assert store.event_count(second) == 2
        recovery = store.recover(second)
        assert recovery["repaired"] == ["vin:VIN00000000000001"]

        stored = store.get_projection(second, "vin:VIN00000000000001")
        replayed = store.replay_projection(second, "vin:VIN00000000000001")
        assert stored["status"] == replayed["status"] == "reserved"
        assert stored["history_len"] == replayed["history_len"] == 2
        assert stored["restore_position"] == replayed["restore_position"] == "active"
        assert store.recover(second)["count"] == 0
    finally:
        second.close()


def test_error_event_classification_is_queryable(con):
    bad_status = store.ingest(con, ev("nonsense", "2026-01-01T00:00:00Z"))
    missing_identity = store.ingest(con, {"status": "active", "ts": "2026-01-01T00:00:00Z"})
    bad_time = store.ingest(con, ev("active", "not-a-time"))

    assert bad_status["error_class"] == "invalid" and not bad_status["retryable"]
    assert missing_identity["error_class"] == "invalid"
    assert bad_time["error_class"] == "invalid"

    log = store.error_log(con)
    assert [entry["error_class"] for entry in log] == ["invalid", "invalid", "invalid"]
    assert all(entry["retryable"] is False for entry in log)

    duplicate = statusline.classify_error(
        sqlite3.IntegrityError("UNIQUE constraint failed: events.event_key")
    )
    assert duplicate == {"error_class": "duplicate", "retryable": False}
    transient = statusline.classify_error(sqlite3.OperationalError("database is locked"))
    assert transient == {"error_class": "transient", "retryable": True}


# ---------------------------------------------------------------------------
# 多车辆独立
# ---------------------------------------------------------------------------
def test_vehicles_fold_independently():
    a = [
        ev("active", "2026-01-01T00:00:00Z", vin="A"),
        ev("withdrawn", "2026-01-02T00:00:00Z", vin="A"),
        ev("relisted", "2026-01-03T00:00:00Z", vin="A"),
    ]
    b = [
        ev("active", "2026-01-01T00:00:00Z", vin="B"),
        ev("reserved", "2026-01-02T00:00:00Z", vin="B"),
        ev("withdrawn", "2026-01-03T00:00:00Z", vin="B"),
    ]
    result = statusline.projections(a + b)
    assert result["vin:A"]["status"] == "active"          # 恢复到撤销前的 active
    assert result["vin:A"]["restore_position"] == "active"
    assert result["vin:B"]["status"] == "withdrawn"       # 保持在撤销态
    assert result["vin:B"]["restore_position"] == "reserved"

    # 单车辆入口只接受一辆车
    with pytest.raises(statusline.InvalidEvent):
        statusline.projection(a + b)


def test_store_projection_isolated_per_vehicle(con):
    store.ingest_many(
        con,
        [
            ev("reserved", "2026-01-01T00:00:00Z", vin="A"),
            ev("withdrawn", "2026-01-02T00:00:00Z", vin="A"),
            ev("sold", "2026-01-01T00:00:00Z", vin="B"),
        ],
    )
    assert store.get_projection(con, "vin:A")["restore_position"] == "reserved"
    assert store.get_projection(con, "vin:B")["status"] == "sold"


# ---------------------------------------------------------------------------
# 摄入顺序无关的持久化状态
# ---------------------------------------------------------------------------
def test_database_state_is_independent_of_ingest_order(tmp_path):
    records = [
        ev("active", "2026-01-01T00:00:00Z"),
        ev("reserved", "2026-01-02T00:00:00Z"),
        ev("withdrawn", "2026-01-03T00:00:00Z"),
        ev("relisted", "2026-01-04T00:00:00Z"),
        ev("sold", "2026-01-05T00:00:00Z", collected="2026-01-05T09:00:00Z"),
        ev("active", "2026-01-05T00:00:00Z", collected="2026-01-05T08:00:00Z"),
    ]

    def snapshot(order, name):
        connection = store.connect(str(tmp_path / f"{name}.db"))
        try:
            store.ingest_many(connection, order)
            proj = store.get_projection(connection, "vin:VIN00000000000001")
            history = [
                row
                for row in connection.execute(
                    "SELECT event_key,status,ordinal,adjudicated_by,conflict_group,"
                    "restore_position FROM events ORDER BY ordinal"
                )
            ]
            # 旧接口在同一进程内仍可用，且与新入口互不干扰
            legacy = pipeline.run()
            return proj, history, legacy["rows"]
        finally:
            connection.close()

    baseline = snapshot(records, "base")
    for index, order in enumerate(
        (
            records[::-1],
            records[2:] + records[:2],
            [records[4], records[0], records[3], records[1], records[5], records[2]],
        )
    ):
        assert snapshot(order, f"perm{index}") == baseline


# ---------------------------------------------------------------------------
# 错误事件的原子性
# ---------------------------------------------------------------------------
def test_failed_ingest_rolls_back_history_and_projection(con):
    store.ingest(con, ev("active", "2026-01-01T00:00:00Z"))
    before_events = store.event_count(con)
    before_projection = store.get_projection(con, "vin:VIN00000000000001")

    # 伪造一个规范化后必然失败的“事件”：状态合法但车辆身份缺失
    receipt = store.ingest(
        con,
        {"event_key": "manual", "status": "sold", "ts": "2026-01-02T00:00:00Z"},
    )
    assert receipt["ok"] is False
    assert store.event_count(con) == before_events
    assert store.get_projection(con, "vin:VIN00000000000001") == before_projection


def test_pipeline_run_events_reports_receipts_and_errors(tmp_path):
    con = store.connect(str(tmp_path / "run.db"))
    try:
        summary = pipeline.run_events(
            records=[
                ev("active", "2026-01-01T00:00:00Z"),
                {"status": "bogus", "ts": "2026-01-02T00:00:00Z", "vin": "V1"},
                ev("sold", "2026-01-03T00:00:00Z"),
            ],
            con=con,
        )
    finally:
        con.close()

    assert summary["fed"] == 3
    assert summary["ingested"] == 2
    assert summary["failed"] == 1
    assert [r["ok"] for r in summary["receipts"]] == [True, False, True]
    assert summary["receipts"][1]["error_class"] == "invalid"
    assert summary["projections"][0]["status"] == "sold"
    assert [e["error_class"] for e in summary["errors"]] == ["invalid"]


# ---------------------------------------------------------------------------
# 旧接口兼容
# ---------------------------------------------------------------------------
def test_legacy_statusline_process_unchanged():
    record = {"id": "x", "status": "sold"}
    out = statusline.process(record)
    assert out == {"id": "x", "status": "sold", "_stage_statusline": "ok"}
    assert "status" in record


def test_legacy_pipeline_still_idempotent():
    a = pipeline.run()
    b = pipeline.run()
    assert a["fed"] == 4
    assert b["rows"] == a["rows"] == 3
