"""单一职责：挂牌状态行（作为流水线阶段）。

保留原有阶段标记行为；当 identity 阶段已产出稳定身份对象时，状态行只做
透传引用，后续状态历史可凭 identity_id 挂到同一物理车辆上。
"""
def process(record):
    rec = dict(record)
    rec.setdefault("_stage_statusline", "ok")
    identity = rec.get("_identity")
    if isinstance(identity, dict) and identity.get("identity_id"):
        rec.setdefault("_status_identity_id", identity["identity_id"])
    return rec
