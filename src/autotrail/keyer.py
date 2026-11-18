"""单一职责：派生稳定主键。

键来源顺序（保持对老格式的向后兼容）：
1. 记录自带 ``id`` —— 基线行为，原样使用；
2. ``vin_identity.key`` —— vin_id 阶段产出的标准身份键。里程观测
   （``mileage_obs``）与原始字段不参与键计算，因此同一 VIN 更新里程不会
   产生新车辆；
3. 兜底：整条记录的确定性哈希（基线行为）。
"""
import hashlib, json

def key_of(record):
    rid = record.get("id")
    if rid:
        return str(rid)
    identity = record.get("vin_identity")
    if isinstance(identity, dict) and identity.get("key"):
        return str(identity["key"])
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
