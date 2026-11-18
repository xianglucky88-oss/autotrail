"""单一职责：挂牌状态事件机（normalize → order/adjudicate → fold → project）。

核心确定性约定
--------------
1. **事件时间优先**：当前状态不是“最后写入者赢”，而是把该车辆的全部事件按
   ``(event_time, collected_at, status_precedence, event_key)`` 排序后做一次纯函数
   折叠。迟到、乱序、重复输入无论以何种顺序 replay，得到同一当前值与同一转移历史。
2. **迟到/回退事件**：事件时间早于当前投影的记录会被插回历史中的正确位置并重新
   折叠。**曾成交、随后又收到更早的在售记录 → 当前状态仍是 ``sold``**：更早的在售
   只是这条挂牌的起点，成交是更晚的事实。只有当那条在售记录的事件时间晚于成交时，
   它才构成重新挂牌（``active``）。答案见 :func:`resolve_late_active_after_sold`。
3. **同一时刻冲突**：先比采集时间；采集时间相同则按固定状态优先级裁决
   （``sold > withdrawn > delisted > reserved > relisted > active``，最后以
   ``event_key`` 兜底）。裁决依据写入事件的 ``adjudicated_by``，冲突分组写入
   ``conflict_group``，随历史一起持久化，供事后查证。
4. **撤销后的恢复位置**：``withdrawn`` 事件保存撤销前的状态（``restore_position``），
   之后的 ``relisted`` 恢复到该位置；无历史时默认恢复到 ``active``。恢复位置与
   事件同事务落库。

旧接口 ``process(record)`` 保持基线行为（复制记录并补 ``_stage_statusline`` 标记），
供尚未改造的调用方继续使用。
"""
from datetime import datetime, timezone
import json

from .keyer import MissingIdentity, event_key_of, vehicle_key_of

# 规范状态
ACTIVE = "active"
RESERVED = "reserved"
SOLD = "sold"
DELISTED = "delisted"
WITHDRAWN = "withdrawn"
RELISTED = "relisted"

STATUSES = (ACTIVE, RESERVED, SOLD, DELISTED, WITHDRAWN, RELISTED)

# 同一事件时刻的冲突优先级：数值大者后应用、最终胜出。
# 成交是终局事实，优先级最高；撤销次之。
STATUS_PRECEDENCE = {
    ACTIVE: 0,
    RELISTED: 1,
    RESERVED: 2,
    DELISTED: 3,
    WITHDRAWN: 4,
    SOLD: 5,
}

_ALIASES = {
    # 在售
    "active": ACTIVE, "for_sale": ACTIVE, "on_sale": ACTIVE, "listed": ACTIVE,
    "available": ACTIVE, "在售": ACTIVE, "挂牌": ACTIVE,
    # 预订
    "reserved": RESERVED, "reserve": RESERVED, "pending": RESERVED,
    "reservation": RESERVED, "预订": RESERVED, "预定": RESERVED,
    # 成交
    "sold": SOLD, "sale": SOLD, "closed": SOLD, "成交": SOLD, "已售": SOLD,
    # 下架
    "delisted": DELISTED, "off_sale": DELISTED, "removed": DELISTED,
    "unlisted": DELISTED, "下架": DELISTED,
    # 撤销
    "withdrawn": WITHDRAWN, "canceled": WITHDRAWN, "cancelled": WITHDRAWN,
    "void": WITHDRAWN, "撤销": WITHDRAWN, "撤回": WITHDRAWN,
    # 重新挂牌
    "relisted": RELISTED, "relist": RELISTED, "relisted_active": RELISTED,
    "重新挂牌": RELISTED, "重新上架": RELISTED, "复牌": RELISTED,
}

DEFAULT_RESTORE_POSITION = ACTIVE


class InvalidEvent(ValueError):
    """无法规范化的事件（不可重试）。"""


def process(record):
    """旧接口：基线阶段标记，保持行为不变。"""
    rec = dict(record)
    rec.setdefault("_stage_statusline", "ok")
    return rec


def canonical_status(value):
    """把外部状态词规范为内部状态；未知状态抛 :class:`InvalidEvent`。"""
    key = str(value or "").strip().lower()
    try:
        return _ALIASES[key]
    except KeyError:
        raise InvalidEvent(f"unknown listing status: {value!r}") from None


def _parse_time(value, field):
    text = str(value or "").strip()
    if not text:
        raise InvalidEvent(f"missing {field}")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidEvent(f"invalid {field}: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_event(record, collected_at=None):
    """把原始记录规范成稳定事件（不排序、不折叠）。

    事件时间取自 ``event_time``/``ts``/``event_at``；采集时间取自（或参数覆盖）
    ``collected_at``/``collected``/``fetched_at``/``ingested_at``。采集时间缺失时
    退回事件时间，保证事件键稳定。
    """
    if not isinstance(record, dict):
        raise InvalidEvent("record must be a mapping")

    try:
        vehicle_key = str(record.get("vehicle_key") or vehicle_key_of(record))
    except MissingIdentity as exc:
        raise InvalidEvent(str(exc)) from None

    event_time = _parse_time(
        record.get("event_time") or record.get("ts") or record.get("event_at"),
        "event_time",
    )
    collected_raw = (
        collected_at
        or record.get("collected_at")
        or record.get("collected")
        or record.get("fetched_at")
        or record.get("ingested_at")
    )
    collected = (
        _parse_time(collected_raw, "collected_at") if collected_raw else event_time
    )

    event = {
        "vehicle_key": vehicle_key,
        "status": canonical_status(record.get("status")),
        "event_time": event_time,
        "collected_at": collected,
        "src": str(record.get("src") or record.get("source") or "").strip().lower(),
        "listing_id": str(
            record.get("listing_id")
            or record.get("listingId")
            or record.get("id")
            or ""
        ).strip(),
        "name": str(record.get("name") or "").strip(),
        "value": record.get("value"),
    }
    event["event_key"] = str(record.get("event_key") or "") or event_key_of(event)
    return event


def _sort_key(event):
    return (
        event["event_time"],
        event["collected_at"],
        STATUS_PRECEDENCE[event["status"]],
        event["event_key"],
    )


def _dedupe(events):
    """按事件键消重；重复时不依赖输入顺序（保留序列化后最小的副本）。"""
    chosen = {}
    for event in events:
        key = event["event_key"]
        blob = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
        current = chosen.get(key)
        if current is None or blob < current[0]:
            chosen[key] = (blob, event)
    return [event for _, event in chosen.values()]


def _normalize_all(events):
    normalized = []
    for event in events:
        if isinstance(event, dict) and event.get("event_key"):
            normalized.append(dict(event))
        else:
            normalized.append(normalize_event(event))
    return normalized


def _prepare(events):
    """统一规范化 → 消重 → 排序 → 按车辆标注。"""
    ordered = _dedupe(_normalize_all(events))
    ordered.sort(key=_sort_key)

    by_vehicle = {}
    for event in ordered:
        by_vehicle.setdefault(event["vehicle_key"], []).append(event)

    annotated = []
    for vehicle_key, group in by_vehicle.items():
        # 每辆车独立折叠状态：撤销恢复位置只在本车历史内传播。
        current = None
        restore = DEFAULT_RESTORE_POSITION
        for event in group:
            event["_state_before"] = current
            if event["status"] == WITHDRAWN:
                if current not in (None, WITHDRAWN):
                    restore = current
                current = WITHDRAWN
            elif event["status"] == RELISTED:
                current = restore or DEFAULT_RESTORE_POSITION
            else:
                current = event["status"]

    for vehicle_key, group in by_vehicle.items():
        restore = DEFAULT_RESTORE_POSITION
        for ordinal, event in enumerate(group, start=1):
            same_time = [e for e in group if e["event_time"] == event["event_time"]]
            statuses = {e["status"] for e in same_time}
            if len(statuses) > 1:
                collected = {e["collected_at"] for e in same_time}
                event["conflict_group"] = f"{vehicle_key}:{event['event_time']}"[:191]
                # 同一时刻多种状态：采集时间不同先按采集时间；否则按固定优先级；
                # 同状态重复观测（键不同）由 event_key 兜底决定先后。
                if len(collected) > 1:
                    event["adjudicated_by"] = "collected_at"
                elif len(statuses) == len(same_time):
                    event["adjudicated_by"] = "status_precedence"
                else:
                    event["adjudicated_by"] = "event_key"
            else:
                event["conflict_group"] = None
                event["adjudicated_by"] = None

            before = event["_state_before"]
            if event["status"] == WITHDRAWN and before not in (None, WITHDRAWN):
                restore = before
            event["restore_position"] = restore
            event["ordinal"] = ordinal
            annotated.append(event)
    return annotated


def annotate_events(events):
    """排序、消重并补全冲突裁决/序号/恢复位置，返回新列表（纯函数）。

    * ``ordinal``          —— 该车辆内的事件顺序（从 1 开始）。
    * ``conflict_group``   —— 同一车辆同一事件时刻出现多种状态时的分组标识。
    * ``adjudicated_by``   —— ``collected_at`` / ``status_precedence`` / ``event_key``。
    * ``restore_position`` —— 应用该事件后，重新挂牌应恢复到的位置。

    输入可以是原始记录或已规范化事件；单独调用也能得到完整标注。
    """
    return _prepare(events)


def _fold(annotated):
    """对已排序、属于同一车辆的事件做一次纯折叠，返回 (current, transitions, restore)。

    重新挂牌（``relisted``）不直接成为状态，而是恢复到最近一次撤销保存的位置。
    """
    current = None
    restore = DEFAULT_RESTORE_POSITION
    transitions = []
    for event in annotated:
        before = current
        if event["status"] == WITHDRAWN:
            restore = event["restore_position"]
            after = WITHDRAWN
        elif event["status"] == RELISTED:
            after = restore or DEFAULT_RESTORE_POSITION
        else:
            after = event["status"]
        current = after
        transitions.append(
            {
                "ordinal": event["ordinal"],
                "event_key": event["event_key"],
                "status": event["status"],
                "before": before,
                "after": after,
                "event_time": event["event_time"],
                "collected_at": event["collected_at"],
                "conflict_group": event["conflict_group"],
                "adjudicated_by": event["adjudicated_by"],
                "restore_position": restore,
            }
        )
    return current, transitions, restore


def _project_one(vehicle_key, annotated):
    if not annotated:
        return {
            "vehicle_key": vehicle_key,
            "status": None,
            "event_key": None,
            "event_time": None,
            "collected_at": None,
            "restore_position": DEFAULT_RESTORE_POSITION,
            "history_len": 0,
            "events": [],
            "transitions": [],
        }

    current, transitions, restore = _fold(annotated)
    last = annotated[-1]
    events_out = [
        {k: v for k, v in event.items() if not k.startswith("_")}
        for event in annotated
    ]
    return {
        "vehicle_key": last["vehicle_key"],
        "status": current,
        "event_key": last["event_key"],
        "event_time": last["event_time"],
        "collected_at": last["collected_at"],
        "restore_position": restore,
        "history_len": len(annotated),
        "events": events_out,
        "transitions": transitions,
    }


def projection(events):
    """单车辆纯折叠：返回当前投影与逐条转移历史。

    ``events`` 可以是原始记录或已规范化事件，内部统一规范化、消重、排序。
    若混入多辆车，抛 :class:`InvalidEvent`——多车请用 :func:`projections`。
    """
    annotated = _prepare(events)
    if not annotated:
        return _project_one(None, [])
    vehicles = {event["vehicle_key"] for event in annotated}
    if len(vehicles) > 1:
        raise InvalidEvent(
            f"projection expects one vehicle, got {len(vehicles)}: "
            f"{sorted(vehicles)}"
        )
    return _project_one(annotated[0]["vehicle_key"], annotated)


def projections(events):
    """多车辆纯折叠：返回 ``{vehicle_key: 投影}``，各车相互独立。"""
    grouped = {}
    for event in _prepare(events):
        grouped.setdefault(event["vehicle_key"], []).append(event)
    return {key: _project_one(key, items) for key, items in grouped.items()}


def resolve_late_active_after_sold(records):
    """明确的确定性答案：成交后收到更早的在售记录，当前状态仍为 ``sold``。

    仅当在售记录的事件时间严格晚于成交时才变为 ``active``（重新挂牌）。
    任意输入顺序结果一致。
    """
    return projection(records)["status"]


def classify_error(exc):
    """错误事件的重试分类。

    * ``duplicate``   —— 事件已存在：无需重试，视为成功。
    * ``invalid``     —— 数据/身份/时间/状态非法：重试也不会成功。
    * ``transient``   —— 锁冲突、IO、超时等：可重试。
    * ``unexpected``  —— 未分类：保守标记可重试并保留原因。
    """
    if isinstance(exc, (InvalidEvent, MissingIdentity, ValueError, TypeError)):
        return {"error_class": "invalid", "retryable": False}

    text = str(exc).lower()
    name = type(exc).__name__.lower()
    if "integrity" in name or "unique constraint" in text:
        return {"error_class": "duplicate", "retryable": False}
    if "operational" in name or "locked" in text or "timeout" in text or "i/o" in text:
        return {"error_class": "transient", "retryable": True}
    return {"error_class": "unexpected", "retryable": True}
