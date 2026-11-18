"""单一职责：事件行阶段。

对照同一来源挂牌的上一版本，输出事件信息（写入 ``_event``），供 dedup
结合身份信息裁决：
- ``new``：首次见到的来源挂牌；
- ``retransmit``：内容指纹与上一版本完全一致的重传；
- ``replay``：时间戳不新于已收录版本的历史记录回放（内容可能不同）；
- ``relist``：状态由失效（sold/expired/removed…）重新变为在售；
- ``edit``：在售期间业务字段发生变化，附带逐字段 diff 作为证据。

事件阶段只描述“发生了什么”，不决定合并还是保留。
"""
from .keyer import source_key_of, content_hash

EVENT_OUT = "_event"

COMPARE_FIELDS = (
    "vin", "mileage", "model", "address", "price", "status", "ts",
    "name", "value",
)

ACTIVE_STATUSES = frozenset({"active", "listed", "on_sale", "onsale"})
INACTIVE_STATUSES = frozenset(
    {"sold", "expired", "removed", "inactive", "off_sale", "offsale"}
)


def is_active(status):
    return str(status or "").strip().lower() in ACTIVE_STATUSES


def _diff(prev, curr):
    changes = {}
    for field in COMPARE_FIELDS:
        old = prev.get(field)
        new = curr.get(field)
        if old != new:
            changes[field] = {"old": old, "new": new}
    return changes


def event_of(record, prev, prev_seq):
    """纯函数：依据当前记录与同来源上一版本计算事件。"""
    if prev is None:
        return {
            "kind": "new",
            "changes": {},
            "prev_seq": None,
            "content_hash": content_hash(record),
        }

    changes = _diff(prev, record)
    same_content = content_hash(record) == content_hash(prev)
    curr_ts = str(record.get("ts") or "").strip()
    prev_ts = str(prev.get("ts") or "").strip()

    if same_content:
        kind = "retransmit"
    elif curr_ts and prev_ts and curr_ts <= prev_ts:
        # 两侧都有可信时间戳且当前记录不更新时，才判定为历史回放；
        # 时间戳缺失时不臆造时序，按普通编辑处理。
        kind = "replay"
    elif not is_active(prev.get("status")) and is_active(record.get("status")):
        kind = "relist"
    else:
        kind = "edit"

    return {
        "kind": kind,
        "changes": changes,
        "prev_seq": prev_seq,
        "content_hash": content_hash(record),
    }


def process(record, ctx=None):
    rec = dict(record)
    skey = source_key_of(rec)
    if ctx is not None and skey in getattr(ctx, "_inflight", set()):
        # pre_index 的占位即当前记录自身；事件判定只认账本里的上一版本。
        prev, prev_seq = ctx.persisted_previous(skey)
    elif ctx is not None:
        prev_seq = ctx.latest_seq(skey)
        prev = ctx.latest_source(skey)
    else:
        prev, prev_seq = None, None
    rec[EVENT_OUT] = event_of(rec, prev, prev_seq)
    rec.setdefault("_stage_statusline", "ok")
    return rec
