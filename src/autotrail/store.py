"""单一职责：本地 sqlite3 持久化，提供幂等 upsert / 计数 / 全量读取。

``upsert`` 默认覆盖写（与基线完全一致）。传入 ``merge_list_fields`` 时，
命中同键的旧行会把这些列表字段按元素去重合并，其余字段以新记录为准——
用于同一身份下累积多来源记录与里程观测，而不是互相覆盖。
"""
import json, sqlite3
from .config import DB_PATH

def connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return con

def _merge_lists(old, new, fields):
    merged = dict(old)
    for fld in fields:
        if fld not in new:
            continue
        combined = list(old.get(fld) or [])
        seen = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in combined}
        for item in new[fld]:
            token = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if token not in seen:
                seen.add(token)
                combined.append(item)
        merged[fld] = combined
    # 非列表字段以新记录为准（身份块等），但保留新记录没有而旧记录有的键
    for k, v in new.items():
        if k not in fields:
            merged[k] = v
    return merged

def upsert(con, record, merge_list_fields=()):
    existing = con.execute("SELECT data FROM records WHERE key=?", (record.key,)).fetchone()
    if existing is not None and merge_list_fields:
        data = _merge_lists(json.loads(existing[0]), record.data, tuple(merge_list_fields))
    else:
        data = record.data
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(data, ensure_ascii=False)))

def commit(con): con.commit()

def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]
