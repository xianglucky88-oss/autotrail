"""单一职责：字段规整/规范化。

旧字段（src/name/value/status/ts）行为保持不变；新增车辆挂牌字段
（vin/mileage/model/address/price）做统一类型与空白规整，字段缺失时
保留 None，交由下游按“字段缺失”规则处置，而不是在这里臆造默认值。
"""


def _clean_str(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value):
    if value is None or value == "":
        return None
    return int(value)


def process(record, ctx=None):
    rec = dict(record)
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = str(rec.get("name", "")).strip()
    rec["value"] = int(rec.get("value", 0))

    rec["vin"] = _clean_str(rec.get("vin"))
    rec["mileage"] = _to_int(rec.get("mileage"))
    rec["model"] = _clean_str(rec.get("model"))
    rec["address"] = _clean_str(rec.get("address"))
    rec["price"] = _to_int(rec.get("price"))
    rec["status"] = _clean_str(rec.get("status"))
    rec["ts"] = _clean_str(rec.get("ts"))
    return rec
