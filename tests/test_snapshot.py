"""快照历史与 as-of 查询测试。

覆盖：先成交后补旧挂牌、同刻不同来源报价、写入一半退出与恢复、下架后重现、
同一时刻多次抓取、价格回撤、缺字段、乱序输入、中途重跑、从历史重建当前视图、
旧接口（records 表）输出保留。
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from src.autotrail import pipeline, snapshot, store
from src.autotrail.models import Record
from src.autotrail import normalizer


def _rec(rid, ts, value=None, status=None, src="alpha", mileage=None, **extra):
    rec = {"id": rid, "src": src, "name": rid}
    if ts is not None:
        rec["ts"] = ts
    if value is not None:
        rec["value"] = value
    if status is not None:
        rec["status"] = status
    if mileage is not None:
        rec["mileage"] = mileage
    rec.update(extra)
    return normalizer.process(rec)


def _snap(rec, captured_at):
    return snapshot.process({**rec, "_snap_capture": captured_at})


@pytest.fixture
def con(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    snapshot.install(c)
    monkeypatch.setattr(store, "connect", lambda: c)  # 让 pipeline 使用同一个内存连接
    yield c
    c.close()


def _put(con, rec, captured_at):
    return snapshot.append(con, _snap(rec, captured_at), captured_at=captured_at)


def test_valid_time_vs_capture_time_are_independent_axes(con):
    """事后补录旧挂牌：valid_time 在过去，captured_at 是现在，互不回改。"""
    _put(con, _rec("v1", "2026-11-10T00:00:00Z", 100, "sold"),
         "2026-11-10T01:00:00Z")
    _put(con, _rec("v1", "2026-11-01T00:00:00Z", 120, "active"),
         "2026-11-17T09:00:00Z")  # 晚到的旧挂牌
    hist = snapshot.history(con, "v1")
    assert [h["valid_time"] for h in hist] == [
        "2026-11-10T00:00:00Z", "2026-11-01T00:00:00Z"]
    backfill = hist[-1]
    assert backfill["captured_at"] == "2026-11-17T09:00:00Z"
    assert backfill["value"] == 120 and backfill["status"] == "active"


def test_sold_first_then_backfill_older_listing_asof(con):
    _put(con, _rec("v2", "2026-11-10T00:00:00Z", 90, "sold"),
         "2026-11-10T01:00:00Z")
    _put(con, _rec("v2", "2026-11-05T00:00:00Z", 110, "active"),
         "2026-11-17T09:00:00Z")
    early = snapshot.as_of(con, "v2", day="2026-11-05")["latest"]
    later = snapshot.as_of(con, "v2", day="2026-11-10")["latest"]
    assert (early["value"], early["status"]) == (110, "active")
    assert (later["value"], later["status"]) == (90, "sold")
    # 补录旧点不回退当前视图
    cur = snapshot.current_state(con, "v2")
    assert (cur["value"], cur["status"], cur["valid_time"]) == (
        90, "sold", "2026-11-10T00:00:00Z")


def test_same_instant_multiple_sources_keep_both_quotes(con):
    t = "2026-11-08T10:00:00Z"
    _put(con, _rec("v3", t, 100, "active", src="alpha"), "2026-11-08T10:05:00Z")
    _put(con, _rec("v3", t, 95, "active", src="beta"), "2026-11-08T10:06:00Z")
    view = snapshot.as_of(con, "v3", at=t)
    assert {r["src"] for r in view["rows"]} == {"alpha", "beta"}
    assert {r["value"] for r in view["rows"]} == {100, 95}
    # 全局决胜：同有效时间取采集时间最新的来源（beta 晚一分钟抓到）
    assert view["latest"]["src"] == "beta"
    assert len(snapshot.history(con, "v3")) == 2


def test_half_written_exit_rolls_back_and_resume_completes(con):
    _put(con, _rec("v4", "2026-11-01T00:00:00Z", 50, "active"),
         "2026-11-01T01:00:00Z")
    con.commit()
    # 模拟写了一半（含快照追加与视图刷新）后进程退出、未 commit
    con.execute("SAVEPOINT half")
    _put(con, _rec("v4", "2026-11-02T00:00:00Z", 45, "active"),
          "2026-11-02T01:00:00Z")
    assert snapshot.current_state(con, "v4")["value"] == 45
    con.execute("ROLLBACK TO SAVEPOINT half")
    con.execute("RELEASE SAVEPOINT half")
    # 未确认部分整体消失，已确认的成交/历史证据保留
    assert snapshot.current_state(con, "v4")["value"] == 50
    assert [h["valid_time"] for h in snapshot.history(con, "v4")] == [
        "2026-11-01T00:00:00Z"]
    # 恢复入口 + 幂等重跑补齐
    info = snapshot.recover(con)
    assert info["vehicles"] == 1 and info["snapshots"] == 1
    _put(con, _rec("v4", "2026-11-02T00:00:00Z", 45, "active"),
         "2026-11-02T01:00:00Z")
    con.commit()
    assert snapshot.current_state(con, "v4")["value"] == 45
    assert len(snapshot.history(con, "v4")) == 2
    # 再重跑不产生重复
    _put(con, _rec("v4", "2026-11-02T00:00:00Z", 45, "active"),
         "2026-11-02T01:00:00Z")
    assert len(snapshot.history(con, "v4")) == 2


def test_delisted_then_relisted(con):
    seq = [
        ("2026-11-01T00:00:00Z", 200, "active"),
        ("2026-11-05T00:00:00Z", None, "delisted"),
        ("2026-11-09T00:00:00Z", 190, "active"),
    ]
    for i, (t, v, s) in enumerate(seq):
        _put(con, _rec("v5", t, v, s), f"2026-11-0{i+1}T12:00:00Z")
    assert snapshot.as_of(con, "v5", day="2026-11-05")["latest"]["status"] == "delisted"
    relist = snapshot.as_of(con, "v5", day="2026-11-09")["latest"]
    assert (relist["status"], relist["value"]) == ("active", 190)
    assert [h["status"] for h in snapshot.history(con, "v5")] == [
        "active", "delisted", "active"]
    assert snapshot.current_state(con, "v5")["status"] == "active"


def test_repeated_scrape_same_instant_is_not_duplicated(con):
    rec = _rec("v6", "2026-11-03T00:00:00Z", 70, "active", mileage=1000)
    assert _put(con, rec, "2026-11-03T00:01:00Z") is True
    # 同一时刻多次抓取：不同采集时间但业务状态完全一致 → 视为同点，不重复
    assert _put(con, rec, "2026-11-03T00:15:00Z") is False
    assert _put(con, rec, "2026-11-03T00:30:00Z") is False
    assert len(snapshot.history(con, "v6")) == 1
    # 价格变化则是新点
    assert _put(con, _rec("v6", "2026-11-03T00:00:00Z", 68, "active",
                          mileage=1000), "2026-11-03T01:00:00Z") is True
    assert len(snapshot.history(con, "v6")) == 2


def test_price_pullback_is_kept_in_order(con):
    for t, v in [("2026-11-01T00:00:00Z", 100),
                 ("2026-11-02T00:00:00Z", 120),
                 ("2026-11-03T00:00:00Z", 90)]:
        _put(con, _rec("v7", t, v, "active"), t.replace("00:00:00", "01:00:00"))
    values = [h["value"] for h in snapshot.history(con, "v7")]
    assert values == [90, 120, 100]  # 时间序列按有效时间倒序，回撤如实保留
    assert snapshot.as_of(con, "v7", day="2026-11-02")["latest"]["value"] == 120
    assert snapshot.as_of(con, "v7", day="2026-11-03")["latest"]["value"] == 90


def test_missing_fields_are_null_not_faked(con):
    rec = _rec("v8", "2026-11-04T00:00:00Z")  # 无 value/status/mileage
    assert rec["value"] is None
    _put(con, rec, "2026-11-04T01:00:00Z")
    row = snapshot.history(con, "v8")[0]
    assert row["value"] is None and row["status"] is None and row["mileage"] is None
    latest = snapshot.as_of(con, "v8", day="2026-11-04")["latest"]
    assert latest["status"] is None and latest["value"] is None


def test_missing_ts_still_persists_and_lands_in_history(con):
    _put(con, _rec("v9", None, 30, "active"), "2026-11-04T01:00:00Z")
    assert snapshot.history(con, "v9")[0]["valid_time"] is None
    # 无有效时间的点不参与 as-of，但当前视图仍可见
    assert snapshot.as_of(con, "v9", day="2026-11-04")["latest"] is None
    assert snapshot.current_state(con, "v9")["value"] == 30


def test_out_of_order_input_and_reruns_stay_consistent(con):
    events = [
        ("2026-11-06T00:00:00Z", 80, "active", "2026-11-06T01:00:00Z"),
        ("2026-11-02T00:00:00Z", 95, "active", "2026-11-07T01:00:00Z"),
        ("2026-11-04T00:00:00Z", 88, "active", "2026-11-08T01:00:00Z"),
    ]
    for t, v, s, cap in events:
        _put(con, _rec("v10", t, v, s), cap)
    # 乱序写入，时间序列仍按有效时间组织
    assert [h["valid_time"] for h in snapshot.history(con, "v10")] == [
        "2026-11-06T00:00:00Z", "2026-11-04T00:00:00Z", "2026-11-02T00:00:00Z"]
    assert snapshot.current_state(con, "v10")["value"] == 80
    # 中途重跑：全部追加再来一遍，行数与视图不变
    for t, v, s, cap in events:
        _put(con, _rec("v10", t, v, s), cap)
    assert len(snapshot.history(con, "v10")) == 3
    assert snapshot.current_state(con, "v10")["value"] == 80


def test_rebuild_current_state_from_history(con):
    for t, v, s in [("2026-11-01T00:00:00Z", 1, "active"),
                    ("2026-11-02T00:00:00Z", 2, "sold")]:
        _put(con, _rec("v11", t, v, s), t)
    con.execute("DELETE FROM current_state")
    assert snapshot.current_state(con, "v11") is None
    snapshot.rebuild_current(con)
    cur = snapshot.current_state(con, "v11")
    assert (cur["value"], cur["status"]) == (2, "sold")
    # 全部车辆视图
    assert {r["vehicle_key"] for r in snapshot.current_state()} == {"v11"}


def test_recover_repairs_diverged_view(con):
    _put(con, _rec("v12", "2026-11-01T00:00:00Z", 60, "active"),
         "2026-11-01T01:00:00Z")
    _put(con, _rec("v12", "2026-11-03T00:00:00Z", 55, "sold"),
         "2026-11-03T01:00:00Z")
    # 人为制造视图与历史不一致（模拟只写了一半的残留状态）
    con.execute("UPDATE current_state SET value=999, status='stale' WHERE vehicle_key='v12'")
    snapshot.recover(con)
    cur = snapshot.current_state(con, "v12")
    assert (cur["value"], cur["status"]) == (55, "sold")


def test_pipeline_writes_snapshots_and_keeps_legacy_records(con, monkeypatch):
    """端到端：快照接入现有 SQLite 提交流程，旧 records 表输出不被破坏。"""
    monkeypatch.setattr(store, "connect", lambda: con)
    fixed = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(snapshot, "clock", lambda: fixed)
    a = pipeline.run(con)
    b = pipeline.run(con)  # 中途重跑：幂等
    assert a["fed"] == 4 and a["rows"] == 3 and a["snapshots"] == 3
    assert b["rows"] == 3 and b["snapshots"] == 0
    # 旧接口输出保留：records 表仍可全量读取且键不变
    keys = {k for k, _ in store.all_records(con)}
    assert keys == {"r-001", "r-002", "r-003"}
    sample = dict(store.all_records(con))["r-001"]
    assert sample["value"] == 10 and sample["status"] == "active"
    assert "_snapshot" in sample
    # 当前视图与历史都能回答 as-of
    cur = snapshot.current_state(con, "r-003")
    assert cur["status"] == "sold"
    view = snapshot.as_of(con, "r-002", day="2026-11-02")
    assert view["latest"]["value"] == 20


def test_snapshot_and_legacy_upsert_are_atomic(monkeypatch):
    """旧 upsert 与快照追加在同一事务：未 commit 整体回滚，两边都不留残行。"""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    snapshot.install(c)
    monkeypatch.setattr(store, "connect", lambda: c)
    c.isolation_level = None
    c.execute("BEGIN")
    rec = snapshot.process(_rec("v13", "2026-11-01T00:00:00Z", 10, "active"))
    store.upsert(c, Record(key="v13", data=rec))
    snapshot.append(c, rec)
    assert store.count(c) == 1 and len(snapshot.history(c, "v13")) == 1
    c.execute("ROLLBACK")  # 模拟写入一半退出、未确认
    assert store.count(c) == 0
    assert snapshot.history(c, "v13") == []
    assert snapshot.current_state(c, "v13") is None
    c.close()


def test_timestamp_normalization_and_day_boundary(con):
    assert snapshot.normalize_ts("2026-11-01T08:00:00+08:00") == "2026-11-01T00:00:00Z"
    assert snapshot.normalize_ts("2026-11-01") == "2026-11-01T00:00:00Z"
    assert snapshot.normalize_ts("") is None
    assert snapshot.normalize_ts("not-a-time") is None
    _put(con, _rec("v14", "2026-11-10T23:30:00Z", 40, "active"),
         "2026-11-10T23:31:00Z")
    # 当天结束前的事件能被按日查询命中
    assert snapshot.as_of(con, "v14", day="2026-11-10")["latest"]["value"] == 40
    assert snapshot.as_of(con, "v14", day="2026-11-09")["latest"] is None
