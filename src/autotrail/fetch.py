"""单一职责：对记录做单一职责变换：fetch。

用于记录变换。"""
def process(record, ctx=None):
    rec = dict(record)
    rec.setdefault("_stage_fetch", "naive")
    return rec
