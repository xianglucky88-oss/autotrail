"""薄编排：loader → normalizer→fetch→identity→statusline→dedup→snapshot → keyer → store。

不含业务判断。每条记录在同一事务内完成旧视图 upsert 与历史快照追加，
由末尾一次 commit 统一确认；中途退出时未确认部分整体回滚，重跑幂等补齐。
"""
import importlib
from . import store as _store
from . import loader as _loader
from . import snapshot as _snapshot
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['normalizer', 'fetch', 'identity', 'statusline', 'dedup', 'snapshot']

def run(con=None):
    own = con is None
    con = con or _store.connect()
    try:
        chain = []
        for n in CHAIN_NAMES:
            mod = importlib.import_module("." + n, __package__)
            chain.append(mod.process)
        _snapshot.install(con)
        fed = 0
        snaps = 0
        for raw in _loader.load():
            rec = raw
            for fn in chain:
                rec = fn(rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            if _snapshot.append(con, rec):
                snaps += 1
            fed += 1
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con), "snapshots": snaps}
    finally:
        if own:
            con.close()
