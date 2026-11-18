"""单一职责：本地 sqlite3 持久化。

三张表分工：

* ``records``      —— 旧的逐记录表（保持基线 ``upsert``/``count`` 行为）。
* ``events``       —— 挂牌事件历史（只增不改，``event_key`` 主键消重）。裁决依据
  ``adjudicated_by``、冲突分组 ``conflict_group``、顺序 ``ordinal`` 与撤销恢复位置
  ``restore_position`` 与事件同事务保存。
* ``projections``  —— 每个车辆身份一行当前投影，由事件历史纯折叠得到。
* ``event_errors`` —— 失败事件的重试分类台账，可查询。

写入确认：:func:`append_event` 返回提交回执（``event_key``/``inserted``），
:func:`commit` 返回本次提交的确认。:func:`recover` 会以事件历史为准重建缺失或
过期的投影，修复“历史已写、投影未更新”的中断。
"""
import json
import sqlite3

from . import statusline
from .config import DB_PATH
from .keyer import event_key_of

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS events(
        event_key TEXT PRIMARY KEY,
        vehicle_key TEXT NOT NULL,
        status TEXT NOT NULL,
        event_time TEXT NOT NULL,
        collected_at TEXT NOT NULL,
        ordinal INTEGER,
        adjudicated_by TEXT,
        conflict_group TEXT,
        restore_position TEXT,
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS projections(
        vehicle_key TEXT PRIMARY KEY,
        status TEXT,
        event_key TEXT,
        event_time TEXT,
        collected_at TEXT,
        restore_position TEXT,
        history_len INTEGER NOT NULL DEFAULT 0,
        frontier TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS event_errors(
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_key TEXT,
        event_key TEXT,
        error_class TEXT NOT NULL,
        retryable INTEGER NOT NULL,
        message TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_events_vehicle ON events(vehicle_key)",
)


def connect(path=None):
    con = sqlite3.connect(path or DB_PATH)
    for statement in _SCHEMA:
        con.execute(statement)
    con.commit()
    return con


# --------------------------------------------------------------------------
# 旧接口：逐记录表
# --------------------------------------------------------------------------
def upsert(con, record):
    con.execute(
        "INSERT INTO records(key,data) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
        (record.key, json.dumps(record.data, ensure_ascii=False)),
    )


def count(con):
    return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]


# --------------------------------------------------------------------------
# 事件历史
# --------------------------------------------------------------------------
def append_event(con, event):
    """追加一条规范化事件（不提交），返回插入回执。"""
    inserted = con.execute(
        "INSERT INTO events(event_key,vehicle_key,status,event_time,collected_at,payload) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING",
        (
            event["event_key"],
            event["vehicle_key"],
            event["status"],
            event["event_time"],
            event["collected_at"],
            json.dumps(event, ensure_ascii=False, sort_keys=True),
        ),
    ).rowcount
    return {
        "event_key": event["event_key"],
        "vehicle_key": event["vehicle_key"],
        "inserted": bool(inserted),
        "outcome": "inserted" if inserted else "duplicate",
    }


def vehicle_events(con, vehicle_key):
    return [
        json.loads(payload)
        for (payload,) in con.execute(
            "SELECT payload FROM events WHERE vehicle_key=? ORDER BY event_key",
            (vehicle_key,),
        )
    ]


def event_count(con):
    return con.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def all_event_keys(con):
    return [key for (key,) in con.execute("SELECT event_key FROM events ORDER BY event_key")]


# --------------------------------------------------------------------------
# 当前投影
# --------------------------------------------------------------------------
def refresh_projection(con, vehicle_key):
    """以事件历史为准重算单个车辆的投影（不提交），返回投影。"""
    events = vehicle_events(con, vehicle_key)
    proj = statusline.projection(events)
    if not events:
        con.execute("DELETE FROM projections WHERE vehicle_key=?", (vehicle_key,))
        return proj

    for event in proj["events"]:
        con.execute(
            "UPDATE events SET ordinal=?, adjudicated_by=?, conflict_group=?, "
            "restore_position=? WHERE event_key=?",
            (
                event["ordinal"],
                event["adjudicated_by"],
                event["conflict_group"],
                event["restore_position"],
                event["event_key"],
            ),
        )

    con.execute(
        "INSERT INTO projections(vehicle_key,status,event_key,event_time,collected_at,"
        "restore_position,history_len,frontier) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(vehicle_key) DO UPDATE SET status=excluded.status,"
        "event_key=excluded.event_key,event_time=excluded.event_time,"
        "collected_at=excluded.collected_at,restore_position=excluded.restore_position,"
        "history_len=excluded.history_len,frontier=excluded.frontier",
        (
            vehicle_key,
            proj["status"],
            proj["event_key"],
            proj["event_time"],
            proj["collected_at"],
            proj["restore_position"],
            proj["history_len"],
            proj["event_key"],
        ),
    )
    return proj


def get_projection(con, vehicle_key):
    row = con.execute(
        "SELECT vehicle_key,status,event_key,event_time,collected_at,restore_position,"
        "history_len FROM projections WHERE vehicle_key=?",
        (vehicle_key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "vehicle_key": row[0],
        "status": row[1],
        "event_key": row[2],
        "event_time": row[3],
        "collected_at": row[4],
        "restore_position": row[5],
        "history_len": row[6],
    }


def all_projections(con):
    return [
        get_projection(con, key)
        for (key,) in con.execute(
            "SELECT vehicle_key FROM projections ORDER BY vehicle_key"
        )
    ]


def replay_projection(con, vehicle_key):
    """确定性核对：直接从事件历史重算，不动数据库。"""
    return statusline.projection(vehicle_events(con, vehicle_key))


# --------------------------------------------------------------------------
# 提交与摄入
# --------------------------------------------------------------------------
def commit(con):
    con.commit()
    return {"committed": True}


def ingest(con, record, do_commit=True):
    """规范化、落历史、更新投影；返回提交回执与错误分类。

    ``record`` 可以是原始记录或已规范化事件。单条事件的“历史写入 + 投影更新”在
    SAVEPOINT 内完成，失败则整体回滚并分类；失败事件不会中断调用方。
    """
    try:
        event = (
            record
            if isinstance(record, dict) and record.get("event_key")
            else statusline.normalize_event(record)
        )
    except Exception as exc:  # noqa: BLE001 - 分类后回执，不中断整批
        return record_error(con, record, exc, do_commit=do_commit)

    con.execute("SAVEPOINT ingest_event")
    try:
        receipt = append_event(con, event)
        proj = refresh_projection(con, event["vehicle_key"])
    except Exception as exc:  # noqa: BLE001 - 回滚本事件并分类
        con.execute("ROLLBACK TO SAVEPOINT ingest_event")
        con.execute("RELEASE SAVEPOINT ingest_event")
        return record_error(con, record, exc, do_commit=do_commit)
    con.execute("RELEASE SAVEPOINT ingest_event")

    if do_commit:
        commit(con)
    return {
        "ok": True,
        "event_key": event["event_key"],
        "vehicle_key": event["vehicle_key"],
        "inserted": receipt["inserted"],
        "outcome": receipt["outcome"],
        "status": proj["status"],
        "committed": bool(do_commit),
    }


def ingest_many(con, records):
    """批量摄入但只在末尾提交一次；返回逐条回执汇总。"""
    results = []
    for record in records:
        results.append(ingest(con, record, do_commit=False))
    if any(r["ok"] for r in results):
        commit(con)
    return {
        "fed": len(results),
        "ingested": sum(1 for r in results if r["ok"]),
        "duplicates": sum(1 for r in results if r.get("outcome") == "duplicate"),
        "failed": sum(1 for r in results if not r["ok"]),
        "results": results,
    }


def record_error(con, record, exc, do_commit=True):
    info = statusline.classify_error(exc)
    vehicle_key = record.get("vehicle_key") if isinstance(record, dict) else None
    event_key = record.get("event_key") if isinstance(record, dict) else None
    con.execute(
        "INSERT INTO event_errors(vehicle_key,event_key,error_class,retryable,message,"
        "occurred_at) VALUES(?,?,?,?,?,?)",
        (
            vehicle_key,
            event_key,
            info["error_class"],
            1 if info["retryable"] else 0,
            f"{type(exc).__name__}: {exc}",
            _utc_now(),
        ),
    )
    if do_commit:
        commit(con)
    return {
        "ok": False,
        "vehicle_key": vehicle_key,
        "event_key": event_key,
        "error_class": info["error_class"],
        "retryable": info["retryable"],
        "message": str(exc),
        "committed": bool(do_commit),
    }


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --------------------------------------------------------------------------
# 中断恢复与错误台账
# --------------------------------------------------------------------------
def recover(con):
    """以事件历史为准重建投影；返回被修复的车辆键。

    覆盖中断窗口：历史已提交、投影未更新（缺失或与历史重算结果不一致）。
    """
    fields = (
        "status",
        "event_key",
        "event_time",
        "collected_at",
        "restore_position",
        "history_len",
    )
    repaired = []
    for (vehicle_key,) in con.execute(
        "SELECT DISTINCT vehicle_key FROM events ORDER BY vehicle_key"
    ).fetchall():
        expected = replay_projection(con, vehicle_key)
        stored = get_projection(con, vehicle_key)
        if stored is None or any(stored[f] != expected[f] for f in fields):
            refresh_projection(con, vehicle_key)
            repaired.append(vehicle_key)
    if repaired:
        commit(con)
    return {"repaired": repaired, "count": len(repaired)}


def error_log(con):
    """可查询的错误事件分类台账。"""
    return [
        {
            "seq": seq,
            "vehicle_key": vehicle_key,
            "event_key": event_key,
            "error_class": error_class,
            "retryable": bool(retryable),
            "message": message,
            "occurred_at": occurred_at,
        }
        for (
            seq,
            vehicle_key,
            event_key,
            error_class,
            retryable,
            message,
            occurred_at,
        ) in con.execute(
            "SELECT seq,vehicle_key,event_key,error_class,retryable,message,occurred_at "
            "FROM event_errors ORDER BY seq"
        )
    ]
