"""单一职责：身份阶段。

为每条规范化挂牌输出身份判断（写入 ``_identity``），供 dedup 决策使用：
- 有 VIN：以 VIN 为车辆身份，置信度 ``vin``，同 VIN 记录进入同一候选桶；
- 无 VIN：以车型+地址+里程段组合出候选身份，置信度 ``composite``，
  组合键只用于“生成候选”，不直接等同同一辆车（最终由 dedup 裁决）；
- 关键字段缺失：置信度 ``weak``，下游只允许保留或待审，禁止自动合并。

身份阶段只负责“输出身份与依据”，不做合并/覆盖决策。
"""
import hashlib

IDENTITY_OUT = "_identity"

_MILEAGE_BUCKET = 5000  # 里程段宽度（公里），无 VIN 时用于候选分桶


def _token(value):
    return str(value).strip().lower() if value not in (None, "") else "unknown"


def _bucket_hash(*parts):
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def identity_of(record):
    vin = record.get("vin")
    model = record.get("model")
    address = record.get("address")
    mileage = record.get("mileage")

    if vin:
        vin_norm = str(vin).strip().upper()
        vehicle_key = "vin:" + vin_norm
        primary_block = "blk:vin:" + vin_norm
        # 有 VIN 的记录同时进入同里程段影子桶：无 VIN 的相似记录可以在
        # 这里发现它，并因证据不对称（对方有 VIN、自己没有）保守待审。
        mileage = record.get("mileage")
        shadow = ("blk:shadow:mileage:"
                  f"{mileage // _MILEAGE_BUCKET}"
                  if isinstance(mileage, int) else None)
        block_keys = [primary_block] + ([shadow] if shadow else [])
        basis = ["vin"]
        confidence = "vin"
    else:
        mileage_bucket = (
            f"{mileage // _MILEAGE_BUCKET}" if isinstance(mileage, int)
            else None
        )
        model_t = _token(model)
        addr_t = _token(address)
        vehicle_key = "veh:cmp:" + _bucket_hash(
            model_t or "unknown", addr_t or "unknown",
            mileage_bucket or "unknown")
        # 主候选桶按里程段粗分：车型/地址相似但不完全相同的记录进入同一桶，
        # “相似但不同车辆”交给 dedup 的逐字段证据裁决。
        primary_block = "blk:cmp:mileage:" + (mileage_bucket or "unknown")
        # 同里程段的 VIN 影子桶：用于发现持有 VIN 的相似候选。
        shadow = ("blk:shadow:mileage:" + mileage_bucket
                  if mileage_bucket else None)
        block_keys = [primary_block] + ([shadow] if shadow else [])
        basis = [f for f, v in (
            ("model", model), ("address", address),
            ("mileage_bucket", mileage_bucket),
        ) if v not in (None, "")]
        confidence = "composite" if len(basis) >= 2 else "weak"

    return {
        "vin": str(vin).strip().upper() if vin else None,
        "vehicle_key": vehicle_key,
        "block_key": primary_block,
        "block_keys": block_keys,
        "confidence": confidence,
        "basis": basis,
        "missing": [f for f in ("vin", "model", "address", "mileage")
                    if record.get(f) in (None, "")],
    }


def process(record, ctx=None):
    rec = dict(record)
    rec[IDENTITY_OUT] = identity_of(rec)
    rec.setdefault("_stage_identity", "ok")
    return rec
