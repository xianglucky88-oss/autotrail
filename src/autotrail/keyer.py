"""单一职责：派生稳定主键。

三把键各司其职：

* ``key_of``         —— 旧的逐记录主键（保留原行为：优先原始 ``id``）。
* ``vehicle_key_of`` —— 车辆身份键：优先 VIN，其次是 ``src:id``。
* ``event_key_of``   —— 挂牌事件身份键：同一车辆、同一状态、同一事件/采集时间
  的同一份观测得到同一把键，用于消重。

键只依赖规范化后的字段，不含自增序号/当前时间，因此重放时稳定。
"""
import hashlib
import json


class MissingIdentity(ValueError):
    """记录不足以确定车辆身份。"""


def key_of(record):
    """旧接口：逐记录主键（保留基线行为）。"""
    rid = record.get("id")
    if rid:
        return str(rid)
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]


def _clean(value):
    if value is None:
        return ""
    return str(value).strip()


def vehicle_key_of(record):
    """车辆身份键。

    已有身份优先：``vin``/``vin_id`` 等字段存在时用 VIN，否则退回
    ``src:id``，最后才是裸 ``id``。不做任何跨源猜测，调用方给什么身份
    就用什么身份。
    """
    for name in ("vin", "vin_id", "vinId", "vehicle_id", "vehicleId"):
        value = _clean(record.get(name)).upper()
        if value:
            return "vin:" + value

    source = _clean(record.get("src") or record.get("source")).lower()
    source_id = _clean(
        record.get("listing_id")
        or record.get("listingId")
        or record.get("id")
    )
    if source_id:
        return f"src:{source}:{source_id}" if source else "src:" + source_id
    raise MissingIdentity("record has neither vin nor source id")


def event_key_of(record):
    """挂牌事件身份键。

    输入可以是原始记录，也可以是 ``statusline.normalize_event`` 后的
    规范化事件；两种输入都能得到稳定结果。
    """
    payload = {
        "vehicle_key": _clean(record.get("vehicle_key")) or vehicle_key_of(record),
        "status": _clean(record.get("status")).lower(),
        "event_time": _clean(
            record.get("event_time") or record.get("ts") or record.get("event_at")
        ),
        "collected_at": _clean(
            record.get("collected_at")
            or record.get("collected")
            or record.get("fetched_at")
            or record.get("ingested_at")
        ),
        "src": _clean(record.get("src") or record.get("source")).lower(),
        "id": _clean(record.get("listing_id") or record.get("id")),
        "value": record.get("value"),
        "name": _clean(record.get("name")),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
