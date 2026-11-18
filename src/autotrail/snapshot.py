"""单一职责：挂牌/成交价历史快照与 as-of 查询。

两条时间轴（双时态）：
* valid_time 有效时间 —— 价格/里程/状态在真实世界生效的时间点，取自记录自带的
  ``ts``（平台事件时间）。as-of 查询只按它回答"某一天这辆车当时挂多少钱、什么状态"。
* captured_at 采集时间 —— 本系统实际抓取并写入该条记录的处理时钟（UTC）。事后补录
  旧挂牌时 valid_time 是过去、captured_at 是现在，两者互不回改。

存储：
* ``snapshots`` 只追加、永不更新；业务指纹作主键保证同一辆车同一时刻同一来源的
  同一状态不可重复写入（重跑/重抓幂等）。
* ``current_state`` 是可从 snapshots 完整重建的当前视图，每车一行，始终等于该车辆
  valid_time 最新的那个点（乱序补录不会把当前视图回退）。

所有写入均在调用方事务内完成，由 ``store.commit`` 统一提交；中途退出时未确认部分
随连接关闭回滚，重跑幂等补齐，``recover`` 负责校验并重建当前视图。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .keyer import key_of

# 测试可通过 snapshot.clock = callable 注入固定时钟；生产留空走系统 UTC 时钟。
clock = None

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_utc():
    """当前采集时间，固定宽度 UTC 'Z' 字符串，保证可按字典序比较。"""
    dt = clock() if callable(clock) else datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime(_TS_FMT)


def normalize_ts(value):
    """把外部时间戳归一成 UTC 'Z' 秒级字符串；缺失/无法解析时返回 None。

    支持 ``...Z``、带偏移量 ``+08:00``、朴素时间（按 UTC 处理）以及纯日期
    （按该日 UTC 零点处理）。固定宽度输出使字符串比较与时间顺序一致。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime(_TS_FMT)


def process(record):
    """流水线阶段：只做快照注解，不触碰存储。"""
    rec = dict(record)
    rec["_snapshot"] = {
        "vehicle_key": key_of(rec),
        "valid_time": normalize_ts(rec.get("ts")),
    }
    return rec


def install(con):
    """建表建索引（幂等）。"""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots(
            snap_id     TEXT PRIMARY KEY,           -- 业务指纹（不含采集时间）：同车/同有效时间/同源/同状态不可重复
            vehicle_key TEXT NOT NULL,              -- 车辆稳定键（keyer 派生）
            valid_time  TEXT,                       -- 有效时间（平台 ts，UTC Z），缺失允许 NULL
            captured_at TEXT NOT NULL,              -- 采集时间（本系统写入时钟，UTC Z）
            src         TEXT NOT NULL,
            value       INTEGER,                    -- 挂牌价/成交价，缺失为 NULL
            mileage     INTEGER,                    -- 里程，缺失为 NULL
            status      TEXT,                       -- 状态，缺失为 NULL
            UNIQUE(vehicle_key, valid_time, src, value, mileage, status)
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_snap_vehicle_valid "
        "ON snapshots(vehicle_key, valid_time, captured_at, src, snap_id)"
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS current_state(
            vehicle_key TEXT PRIMARY KEY,
            valid_time  TEXT,
            captured_at TEXT NOT NULL,
            src         TEXT NOT NULL,
            value       INTEGER,
            mileage     INTEGER,
            status      TEXT,
            updated_at  TEXT NOT NULL
        )
        """
    )


def _fingerprint(vehicle_key, valid_time, src, value, mileage, status):
    # 采集时间刻意不参与指纹：同一业务状态在不同时刻被重复抓取只能算一个点。
    payload = [vehicle_key, valid_time, src, value, mileage, status]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_from_record(record, captured_at):
    note = record.get("_snapshot") or {}
    vehicle_key = note.get("vehicle_key") or key_of(record)
    valid_time = note.get("valid_time")
    if valid_time is None and "ts" in record:
        valid_time = normalize_ts(record.get("ts"))
    return {
        "vehicle_key": vehicle_key,
        "valid_time": valid_time,
        "captured_at": captured_at,
        "src": str(record.get("src") or ""),
        "value": record.get("value"),
        "mileage": record.get("mileage"),
        "status": record.get("status"),
    }


def append(con, record, captured_at=None):
    """追加一条快照并刷新该车当前视图，同一事务内完成。

    靠 snap_id 指纹 ``INSERT OR IGNORE`` 实现幂等：重跑、同一时刻重复抓取不会产生
    重复行；不同来源或同刻不同报价因指纹不同而各自保留。返回 True 表示新写入。
    """
    install(con)
    captured_at = captured_at or now_utc()
    row = _row_from_record(record, captured_at)
    snap_id = _fingerprint(
        row["vehicle_key"], row["valid_time"],
        row["src"], row["value"], row["mileage"], row["status"],
    )
    cur = con.execute(
        """
        INSERT OR IGNORE INTO snapshots(
            snap_id, vehicle_key, valid_time, captured_at, src, value, mileage, status
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (snap_id, row["vehicle_key"], row["valid_time"], row["captured_at"],
         row["src"], row["value"], row["mileage"], row["status"]),
    )
    inserted = cur.rowcount > 0
    if inserted:
        _refresh_one(con, row["vehicle_key"], captured_at)
    return inserted


# 当前视图取点的全局排序：有效时间新者优先（NULL 垫底），再比采集时间、来源、指纹。
_ORDER = "valid_time IS NULL, valid_time DESC, captured_at DESC, src ASC, snap_id ASC"


def _refresh_one(con, vehicle_key, updated_at):
    con.execute(
        f"""
        INSERT INTO current_state(
            vehicle_key, valid_time, captured_at, src, value, mileage, status, updated_at
        )
        SELECT vehicle_key, valid_time, captured_at, src, value, mileage, status, ?
        FROM snapshots
        WHERE vehicle_key = ?
        ORDER BY {_ORDER}
        LIMIT 1
        ON CONFLICT(vehicle_key) DO UPDATE SET
            valid_time=excluded.valid_time,
            captured_at=excluded.captured_at,
            src=excluded.src,
            value=excluded.value,
            mileage=excluded.mileage,
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (updated_at, vehicle_key),
    )


def rebuild_current(con):
    """从只追加的 snapshots 完整重建 current_state 视图。"""
    install(con)
    con.execute("DELETE FROM current_state")
    con.execute(
        f"""
        INSERT INTO current_state(
            vehicle_key, valid_time, captured_at, src, value, mileage, status, updated_at
        )
        SELECT vehicle_key, valid_time, captured_at, src, value, mileage, status, captured_at
        FROM (
            SELECT *, row_number() OVER (
                PARTITION BY vehicle_key ORDER BY {_ORDER}
            ) AS rn
            FROM snapshots
        )
        WHERE rn = 1
        """
    )


def recover(con):
    """崩溃/半写后的恢复入口。

    未 COMMIT 的写入随连接关闭已回滚；这里确保表结构存在、以历史为准重建当前视图，
    调用方随后用原输入重跑 append（幂等）即可补齐未确认部分。
    """
    install(con)
    rebuild_current(con)
    snap_total = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    view_total = con.execute("SELECT COUNT(*) FROM current_state").fetchone()[0]
    return {"snapshots": snap_total, "vehicles": view_total, "repaired": True}


def _to_row(r):
    return {
        "snap_id": r[0],
        "vehicle_key": r[1],
        "valid_time": r[2],
        "captured_at": r[3],
        "src": r[4],
        "value": r[5],
        "mileage": r[6],
        "status": r[7],
    }


_COLS = "snap_id, vehicle_key, valid_time, captured_at, src, value, mileage, status"


def history(con, vehicle_key):
    """同一车辆不可重复的时间序列：按有效时间、采集时间、来源定序，NULL 时间垫底。"""
    install(con)
    rows = con.execute(
        f"SELECT {_COLS} FROM snapshots WHERE vehicle_key=? "
        f"ORDER BY {_ORDER}",
        (vehicle_key,),
    ).fetchall()
    return [_to_row(r) for r in rows]


def current_state(con=None, vehicle_key=None):
    """读取当前视图；不传 vehicle_key 时返回全部车辆。

    con 缺省时打开配置中的默认数据库连接（调用方负责关闭）。
    """
    own = con is None
    if own:
        from . import store as _store
        con = _store.connect()
    install(con)
    try:
        if vehicle_key is None:
            rows = con.execute(
                "SELECT vehicle_key, valid_time, captured_at, src, value, mileage, status "
                "FROM current_state ORDER BY vehicle_key"
            ).fetchall()
            return [dict(zip(("vehicle_key", "valid_time", "captured_at", "src",
                              "value", "mileage", "status"), r)) for r in rows]
        r = con.execute(
            "SELECT vehicle_key, valid_time, captured_at, src, value, mileage, status "
            "FROM current_state WHERE vehicle_key=?",
            (vehicle_key,),
        ).fetchone()
        if r is None:
            return None
        return dict(zip(("vehicle_key", "valid_time", "captured_at", "src",
                         "value", "mileage", "status"), r))
    finally:
        if own:
            con.close()


def _as_of_point(day, at):
    if at is not None:
        point = normalize_ts(at)
        if point is None:
            raise ValueError(f"unparsable as-of timestamp: {at!r}")
        return point
    if day is not None:
        date_part = normalize_ts(day)
        if date_part is None:
            raise ValueError(f"unparsable day: {day!r}")
        # 按日查询取该日 23:59:59（含当天全部事件）
        return date_part[:10] + "T23:59:59Z"
    return now_utc()


def as_of(con, vehicle_key, day=None, at=None):
    """as-of 查询：某一天/某一时刻车辆当时的挂牌价格与状态（含各来源报价）。

    只看 valid_time <= 查询点的快照，取其中有效时间最新的一组（同一有效时间的
    不同来源报价全部返回），``latest`` 为跨来源按全局排序决出的单点。
    """
    install(con)
    point = _as_of_point(day, at)
    rows = con.execute(
        f"""
        SELECT {_COLS} FROM snapshots
        WHERE vehicle_key=? AND valid_time IS NOT NULL AND valid_time <= ?
        ORDER BY {_ORDER}
        """,
        (vehicle_key, point),
    ).fetchall()
    if not rows:
        return {"vehicle_key": vehicle_key, "as_of": point, "rows": [], "latest": None}
    latest_valid = rows[0][2]
    group = [_to_row(r) for r in rows if r[2] == latest_valid]
    return {
        "vehicle_key": vehicle_key,
        "as_of": point,
        "rows": group,
        "latest": _to_row(rows[0]),
    }
