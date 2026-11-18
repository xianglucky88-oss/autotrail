"""单一职责：派生稳定主键与确定性指纹。

- key_of：落 records 表的稳定键。有来源 id 时沿用旧版（旧结果兼容），
  否则按规范化内容哈希。
- content_hash：对参与决策的业务字段做指纹，供重传判定与决策 ID 派生，
  保证历史记录回放时决策 ID 稳定（结果稳定且可重放）。
- decision_id：决策内容的确定性 ID；同输入回放得到同一 ID，落库幂等。
"""
import hashlib
import json

# 参与身份/事件判定的业务字段；与决策相关的辅助字段（_stage_* 等）不参与指纹。
DECISION_FIELDS = (
    "id", "src", "vin", "mileage", "model", "address",
    "price", "status", "ts",
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def key_of(record):
    rid = record.get("id")
    if rid:
        return str(rid)
    payload = {k: record.get(k) for k in DECISION_FIELDS}
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()[:16]


def source_key_of(record):
    """来源挂牌的稳定键：同一来源的同一挂牌 id 的重传/编辑归并到同一来源。"""
    rid = record.get("id")
    if rid:
        return str(rid)
    src = str(record.get("src", "")).strip().lower() or "unknown-src"
    return "src:" + src + ":" + key_of(record)


def content_hash(record):
    """只对业务字段取指纹：辅助字段（_stage_*/_identity/_event/_dedup…）
    不参与，保证同一来源内容在任意处理阶段、任意重跑轮次指纹稳定。"""
    payload = {k: record.get(k) for k in DECISION_FIELDS}
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def decision_id(source_key, seq, digest):
    """确定性决策 ID：同一来源版本的同一决策重放时 ID 不变。"""
    raw = f"{source_key}|{seq}|{digest}"
    return "dec_" + hashlib.sha256(raw.encode()).hexdigest()[:16]
