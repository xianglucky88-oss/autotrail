"""薄编排：loader → normalizer→fetch→identity→statusline→dedup → keyer → store。不含业务判断。

``run`` 保持基线逐记录路径不变；``run_events`` 是事件化入口：以车辆身份为键，
把每条记录落到事件历史并刷新当前投影，回执中带提交确认与错误分类。
"""
import importlib

from . import loader as _loader
from . import statusline as _statusline
from . import store as _store
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['normalizer', 'fetch', 'identity', 'statusline', 'dedup']


def run(con=None):
    own = con is None
    con = con or _store.connect()
    try:
        chain = []
        for n in CHAIN_NAMES:
            mod = importlib.import_module("." + n, __package__)
            chain.append(mod.process)
        fed = 0
        for raw in _loader.load():
            rec = raw
            for fn in chain:
                rec = fn(rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            fed += 1
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()


def run_events(records=None, con=None, collected_at=None):
    """事件化摄入：规范化 → 事件历史 → 当前投影，末尾统一提交。

    ``records`` 缺省读 fixtures。返回逐条回执、提交确认与错误分类汇总；
    每条回执与输入一一对应（非法记录回执 ``ok=False`` 并带分类）。
    """
    own = con is None
    con = con or _store.connect()
    try:
        raw_records = _loader.load() if records is None else records
        events = []
        receipts = []
        for raw in raw_records:
            rec = raw
            for name in CHAIN_NAMES:
                if name == "statusline":
                    continue
                rec = importlib.import_module("." + name, __package__).process(rec)
            try:
                event = _statusline.normalize_event(rec, collected_at=collected_at)
            except Exception as exc:  # noqa: BLE001 - 逐条分类，不中断整批
                info = _statusline.classify_error(exc)
                logged = _store.record_error(con, rec, exc, do_commit=False)
                receipts.append(
                    {
                        "ok": False,
                        "event_key": None,
                        "vehicle_key": logged["vehicle_key"],
                        **info,
                        "message": str(exc),
                        "committed": logged["committed"],
                    }
                )
                continue
            events.append(event)
            receipts.append(
                {
                    "ok": True,
                    "event_key": event["event_key"],
                    "vehicle_key": event["vehicle_key"],
                }
            )

        summary = _store.ingest_many(con, events)
        summary["fed"] = len(raw_records)
        summary["invalid"] = len(raw_records) - len(events)
        summary["failed"] += summary["invalid"]
        successful = iter(summary["results"])
        for position, receipt in enumerate(receipts):
            if not receipt["ok"]:
                continue
            result = next(successful)
            receipts[position] = {
                **receipt,
                "inserted": result["inserted"],
                "outcome": result["outcome"],
                "status": result["status"],
                "committed": result["committed"],
            }
        summary["receipts"] = receipts
        summary["projections"] = _store.all_projections(con)
        summary["errors"] = _store.error_log(con)
        return summary
    finally:
        if own:
            con.close()
