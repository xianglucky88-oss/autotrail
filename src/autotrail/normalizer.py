"""单一职责：字段规整/规范化（对记录做单一职责变换：normalizer）。规范阶段。

缺字段保持 None 而不是伪造 0/空串，后续快照阶段才能区分"没抓到"与"真实值"。
"""

def _int_or_none(v):
    if v is None or v == "":
        return None
    return int(v)

def process(record):
    rec = dict(record)
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = str(rec.get("name", "")).strip()
    rec["value"] = _int_or_none(rec.get("value"))
    if "mileage" in rec:
        rec["mileage"] = _int_or_none(rec.get("mileage"))
    if "status" in rec:
        rec["status"] = None if rec.get("status") in (None, "") else str(rec.get("status")).strip().lower()
    return rec
