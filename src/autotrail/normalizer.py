"""单一职责：字段规整/规范化（对记录做单一职责变换：normalizer）。规范阶段。

保留基线的 src/name/value 处理；对车辆挂牌字段做防御性规整，
身份归并所需的比对规范化（小写、空白折叠等）由 identity 阶段负责。
"""

def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def process(record):
    rec = dict(record)
    rec["src"] = str(rec.get("src", "")).strip().lower()
    if rec.get("name") is not None:
        rec["name"] = str(rec.get("name", "")).strip()
    if rec.get("value") is not None:
        rec["value"] = int(rec.get("value", 0))
    # 车辆字段：存在才规整，不向前置流水线注入新键
    if rec.get("year") is not None:
        rec["year"] = _as_int(rec.get("year"))
    if rec.get("mileage") is not None:
        mileage = _as_int(rec.get("mileage"))
        if mileage is not None:
            rec["mileage"] = mileage
    if rec.get("price") is not None:
        try:
            rec["price"] = float(rec.get("price"))
        except (TypeError, ValueError):
            pass
    for key in ("make", "model", "trim", "dealer", "plate", "vin"):
        if rec.get(key) is not None:
            rec[key] = str(rec[key]).strip()
    return rec
