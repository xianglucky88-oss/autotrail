"""单一职责：本地 sqlite3 持久化。

三张表：
- records：与旧版兼容的稳定键 upsert 视图（键由 keyer 决定），data 为带决策标注的最新记录。
- listing_sources：来源挂牌的只追加日志（每次重传/编辑都留痕，永不覆盖，撤销后来源可恢复）。
- decisions：去重决策的只追加账本（合并/保留/待审 + 依据证据），撤销即追加反向决策。
"""
import json
import sqlite3
from .config import DB_PATH


def connect(db_path=None):
    con = sqlite3.connect(str(db_path or DB_PATH))
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(
        "CREATE TABLE IF NOT EXISTS records("
        "key TEXT PRIMARY KEY, data TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS listing_sources("
        "source_key TEXT NOT NULL,"
        "seq INTEGER NOT NULL,"
        "ingest_ts TEXT NOT NULL,"
        "data TEXT NOT NULL,"
        "is_current INTEGER NOT NULL DEFAULT 1,"
        "PRIMARY KEY(source_key, seq))"
    )
    # 兼容旧库：补齐 is_current 列。
    cols = [r[1] for r in con.execute("PRAGMA table_info(listing_sources)")]
    if "is_current" not in cols:
        con.execute(
            "ALTER TABLE listing_sources ADD COLUMN is_current "
            "INTEGER NOT NULL DEFAULT 1")
    con.execute(
        "CREATE TABLE IF NOT EXISTS decisions("
        "decision_id TEXT PRIMARY KEY,"
        "created_ts TEXT NOT NULL,"
        "source_key TEXT NOT NULL,"
        "source_seq INTEGER NOT NULL DEFAULT 0,"
        "outcome TEXT NOT NULL,"
        "reason TEXT NOT NULL,"
        "target_group TEXT,"
        "evidence TEXT NOT NULL,"
        "policy TEXT NOT NULL,"
        "undone_by TEXT,"
        "supersedes TEXT)"
    )
    # 兼容旧库：补齐 source_seq 列。
    dcols = [r[1] for r in con.execute("PRAGMA table_info(decisions)")]
    if "source_seq" not in dcols:
        con.execute(
            "ALTER TABLE decisions ADD COLUMN source_seq "
            "INTEGER NOT NULL DEFAULT 0")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_decisions_source "
        "ON decisions(source_key)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_decisions_group "
        "ON decisions(target_group)"
    )
    return con


def upsert(con, record):
    con.execute(
        "INSERT INTO records(key,data) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
        (record.key, json.dumps(record.data, ensure_ascii=False)),
    )


def append_source(con, source_key, seq, ingest_ts, data,
                  is_current=True):
    """只追加一条来源挂牌；同一 (source_key, seq) 重放为幂等 no-op。"""
    con.execute(
        "INSERT OR IGNORE INTO listing_sources(source_key,seq,ingest_ts,"
        "data,is_current) VALUES(?,?,?,?,?)",
        (source_key, seq, ingest_ts,
         json.dumps(data, ensure_ascii=False), 1 if is_current else 0),
    )


def latest_source_seq(con, source_key):
    row = con.execute(
        "SELECT MAX(seq) FROM listing_sources WHERE source_key=? "
        "AND is_current=1", (source_key,),
    ).fetchone()
    return row[0] if row and row[0] is not None else 0


def get_source(con, source_key, seq=None):
    if seq is None:
        row = con.execute(
            "SELECT data FROM listing_sources WHERE source_key=? "
            "AND is_current=1 ORDER BY seq DESC LIMIT 1",
            (source_key,),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT data FROM listing_sources WHERE source_key=? AND seq=?",
            (source_key, seq),
        ).fetchone()
    return json.loads(row[0]) if row else None


def iter_sources(con):
    """全部来源版本（含历史回放），按摄取顺序。"""
    rows = con.execute(
        "SELECT source_key, seq, data FROM listing_sources "
        "ORDER BY ingest_ts, source_key, seq"
    )
    for source_key, seq, data in rows:
        yield source_key, seq, json.loads(data)


def iter_current_sources(con):
    """每个来源的当前版本（排除历史回放），供会话装载候选视图。"""
    rows = con.execute(
        "SELECT source_key, seq, data FROM listing_sources "
        "WHERE is_current=1 ORDER BY ingest_ts, source_key, seq"
    )
    for source_key, seq, data in rows:
        yield source_key, seq, json.loads(data)


def append_decision(con, decision):
    con.execute(
        "INSERT OR IGNORE INTO decisions(decision_id,created_ts,source_key,"
        "source_seq,outcome,reason,target_group,evidence,policy,"
        "undone_by,supersedes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            decision["decision_id"],
            decision["created_ts"],
            decision["source_key"],
            decision.get("source_seq", 0),
            decision["outcome"],
            decision["reason"],
            decision.get("target_group"),
            json.dumps(decision.get("evidence", []), ensure_ascii=False),
            json.dumps(decision.get("policy", {}), ensure_ascii=False),
            decision.get("undone_by"),
            decision.get("supersedes"),
        ),
    )


_DECISION_COLS = (
    "decision_id,created_ts,source_key,source_seq,outcome,reason,"
    "target_group,evidence,policy,undone_by,supersedes"
)


def _row_to_decision(row):
    return {
        "decision_id": row[0],
        "created_ts": row[1],
        "source_key": row[2],
        "source_seq": row[3],
        "outcome": row[4],
        "reason": row[5],
        "target_group": row[6],
        "evidence": json.loads(row[7]),
        "policy": json.loads(row[8]),
        "undone_by": row[9],
        "supersedes": row[10],
    }


def get_decision(con, decision_id):
    row = con.execute(
        f"SELECT {_DECISION_COLS} FROM decisions WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    return _row_to_decision(row) if row else None


def decisions_for(con, source_key):
    rows = con.execute(
        f"SELECT {_DECISION_COLS} FROM decisions "
        "WHERE source_key=? ORDER BY created_ts",
        (source_key,),
    )
    return [_row_to_decision(row) for row in rows]


def all_decisions(con):
    rows = con.execute(
        f"SELECT {_DECISION_COLS} FROM decisions ORDER BY created_ts"
    )
    return [_row_to_decision(row) for row in rows]


def mark_undone(con, decision_id, undo_id):
    con.execute(
        "UPDATE decisions SET undone_by=? WHERE decision_id=?",
        (undo_id, decision_id),
    )


def commit(con):
    con.commit()


def count(con):
    return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def all_records(con):
    return [(k, json.loads(d)) for k, d in
            con.execute("SELECT key,data FROM records")]
