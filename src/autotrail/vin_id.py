"""单一职责：VIN/里程身份解析（对单条记录做确定性变换，不做跨记录有状态归并）。

输入：采集记录，可能携带 ``vin`` / ``mileage`` / ``src`` / 挂牌属性等字段，
VIN 可能带分隔符（``-``、空格、点、斜杠、下划线、反斜杠）、大小写不一、
被遮挡（``*``/``#``/``?``，遮挡位统一归一为 ``*``）、只露出一段，或缺失。

输出（在原记录副本上追加，不覆盖原始字段）：
- ``vin_identity``：标准身份块。区分四类键（详见下），全部由记录内容确定性派生；
- ``mileage_obs``：里程观测列表，与身份块**分开存放**，永不参与主键；
- ``sources``：来源记录（原始标识保留，可审计）；
- ``field_claims``：属性字段冲突的留痕，每条冲突保留全部来源取值与确定性裁定。

四类主键（同输入 ⇒ 同键，跨来源重复运行得到同一主键）：
1. ``vin:<17位>``      完整且通过校验的 VIN —— 唯一的强车辆键，可直接作为车辆主键；
2. ``vinpart:<…>``     遮挡/截断的部分 VIN —— 弱键，只在同来源或带完整 VIN 补齐时
                       才能确认同车，禁止把不同完整 VIN 自动并车；
3. ``vinbad:<hash>``   17 位但校验失败/含非法字符 —— 不可作为车辆键，仅用于
                       把“同一条坏 VIN 的多次提交”聚到一起，等待补号；
4. ``fallback:<hash>`` 缺 VIN 时的降级身份，仅由稳定挂牌属性（不含里程）派生，
                       只能在降级域内作键，跨来源不可直接视为同车。

里程只进入 ``mileage_obs``：同一完整 VIN 换里程仍是同一主键，避免用变化的里程
制造新车辆。无 VIN/里程等车辆字段的老格式记录原样透传，继续走原流水线。
"""
import hashlib

# ---- 公开常量（供 keyer/store/pipeline 复用，避免键规则散落多处）----------------

MERGE_LIST_FIELDS = ("mileage_obs", "sources", "field_claims")

VIN_LEN = 17
# VIN 字母表：不含 I/O/Q（易与 1/0 混淆）
_VIN_ALPHABET = set("0123456789ABCDEFGHJKLMNPRSTUVWXYZ")
_SEPARATORS = set(" -./\\_")
# 遮挡记号：``* # ?``。注意大小写 X 都是合法 VIN 字符（大写转写值 7），不当遮挡
_MASK_CHARS = set("*#?")
# 转写值：字母 → 数值（标准 SAE J853 / GB 16735 算法）
def _build_translit():
    table = {}
    letter_groups = ("AJS", "BKT", "CLU", "DMV", "ENW", "FOX", "GPY", "HQZ", "IR")
    for value, group in enumerate(letter_groups, start=1):
        for letter in group:
            table[letter] = value
    for digit in "0123456789":
        table[digit] = int(digit)
    return table

_TRANSLIT = _build_translit()
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)

# 降级身份允许使用的稳定挂牌属性（里程等易变字段刻意不在其中）。
# plate（车牌）单列：它是挂牌场景下最稳定的人工标识，优先单独作为降级依据；
# 无车牌时才退回其余挂牌属性组合。
FALLBACK_PLATE = "plate"
FALLBACK_FIELDS = ("plate", "name", "make", "model", "color", "year")
# 字段冲突留痕覆盖的属性字段
CLAIM_FIELDS = ("plate", "name", "make", "model", "color", "year", "status")


def _hash12(*parts):
    """对若干字符串做确定性短哈希（同输入恒同输出，重复运行/重启不变）。"""
    return hashlib.sha256("\u0000".join(parts).encode("utf-8")).hexdigest()[:12]


# ---- VIN 规范化与校验 -----------------------------------------------------------

def normalize_vin(raw):
    """规范大小写与分隔符，返回 ``(clean, masked, check)``。

    - 非字符串/空输入 ⇒ ``("", False, None)``；
    - 去除分隔符并转大写；含遮挡字符 ⇒ ``masked=True``（遮挡位统一记为 ``*``）；
    - 完整 17 位且无遮挡时执行校验位算法，``check`` 为 True/False；
      其余情况 ``check=None``（无法/不应做校验位判断）。
    """
    if not isinstance(raw, str):
        return "", False, None
    chars = []
    masked = False
    for ch in raw.strip():
        if ch in _SEPARATORS:
            continue
        if ch in _MASK_CHARS:
            masked = True
            chars.append("*")
            continue
        chars.append(ch.upper())
    clean = "".join(chars)
    if not clean:
        return "", False, None
    if masked or len(clean) != VIN_LEN:
        return clean, masked, None
    return clean, False, validate_vin(clean)


def validate_vin(vin):
    """标准 VIN 校验位算法：17 位、无 I/O/Q、第 9 位与加权和模 11 一致。"""
    if not isinstance(vin, str) or len(vin) != VIN_LEN:
        return False
    if any(c not in _VIN_ALPHABET for c in vin):
        return False
    total = 0
    for i, c in enumerate(vin):
        total += _TRANSLIT[c] * _WEIGHTS[i]
    check = total % 11
    expected = "X" if check == 10 else str(check)
    return vin[8] == expected


def _masked_skeleton(clean):
    """遮挡 VIN 的骨架：只保留可见字符与各自位置，避免不同长度误撞。"""
    return ",".join(f"{i}:{c}" for i, c in enumerate(clean) if c != "*")


# ---- 字段冲突裁定（确定性、可审计）----------------------------------------------

def resolve_field_claims(record):
    """对挂牌属性的多来源冲突做确定性裁定。

    规则：全部非空取值留痕；裁定值取“观测时间最新（ts），时间并列时来源名
    字典序最小”的取值，无时间字段则按 (src, 字段名) 稳定排序取首条。
    返回 ``(claims, canonical)``，两者都写进记录，原始字段不被覆盖。
    """
    claims, canonical = [], {}
    src = str(record.get("src", "")).strip().lower()
    ts = str(record.get("ts", ""))
    for fld in CLAIM_FIELDS:
        val = record.get(fld)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        claims.append({"field": fld, "value": str(val).strip(), "src": src, "ts": ts})
    # 确定性裁定：有 ts 的优先于无 ts；时间新者优先；并列时来源名字典序最小
    ordered = sorted(
        claims,
        key=lambda c: (c["ts"] == "", _neg_ts(c["ts"]), c["src"], c["field"]),
    )
    for c in ordered:
        canonical.setdefault(c["field"], c["value"])
    return claims, canonical


def _neg_ts(ts):
    """时间戳取反用于“最新优先”排序；非 ISO 时间退化为字典序安全比较。"""
    # ISO-8601 字符串字典序即时间序；取逐字符反序即可得到“新者在前”的确定性序
    return tuple(-ord(c) for c in ts)


# ---- 降级身份 -------------------------------------------------------------------

def _fallback(record, canonical):
    """缺 VIN（或 VIN 不可信）时的可审计降级身份。

    仅使用稳定挂牌属性（绝不含里程），并显式记录采用了哪些字段：
    - 有车牌：以车牌单独成键（车牌比颜色等描述字段稳定，便于补号后桥接）；
    - 无车牌：用 name/make/model/color/year 的非空组合；
    - 全无：退化为来源域键，标记为最弱档。
    降级键一律 ``cross_source_safe=False``：只能在降级域内作键，
    跨来源不能仅凭它直接宣布同车。
    """
    def _val(fld):
        val = canonical.get(fld) or record.get(fld)
        if val is None:
            return None
        text = str(val).strip()
        return text or None

    plate = _val(FALLBACK_PLATE)
    if plate:
        basis = {FALLBACK_PLATE: plate}
        basis_note = "plate"
        material = "plate=" + plate
    else:
        basis = {fld: _val(fld) for fld in FALLBACK_FIELDS if _val(fld)}
        if basis:
            basis_note = "stable-listing-fields:" + ",".join(sorted(basis))
            material = "|".join(f"{k}={basis[k]}" for k in sorted(basis))
        else:
            basis_note = "src-only"
            material = "src=" + str(record.get("src", "")).strip().lower()
    return {
        "kind": "fallback",
        "key": "fallback:" + _hash12(material),
        "basis_fields": sorted(basis) if basis else ["src"],
        "basis_note": basis_note,
        "cross_source_safe": False,
    }


# ---- 主入口 ---------------------------------------------------------------------

def process(record):
    """对单条记录做 VIN/里程身份解析，返回追加身份块后的新记录（不修改入参）。"""
    rec = dict(record)

    # 老格式透传：只有通用字段（id/src/name/value/status/ts）、没有任何车辆标识
    # 信号（VIN/里程/车牌/车辆属性）时，保持原记录结构与旧流水线行为完全一致
    vehicle_signals = ("vin", "mileage", "odo", "plate", "make", "model", "color", "year")
    if not any(k in rec for k in vehicle_signals):
        return rec

    src = str(rec.get("src", "")).strip().lower()
    raw_vin = rec.get("vin")
    clean, masked, check = normalize_vin(raw_vin)

    identity = {
        "raw_vin": raw_vin if isinstance(raw_vin, str) else (None if raw_vin is None else str(raw_vin)),
        "normalized_vin": clean or None,
        "masked": masked,
        "check": check,
        "check_error": None,
        "kind": None,
        "key": None,
        "usable_as_vehicle_key": False,
        "fallback": None,
        "completion_hint": None,
    }

    if clean and not masked and len(clean) == VIN_LEN and check is True:
        # 1) 完整且通过校验：强车辆键
        identity.update(kind="full", key="vin:" + clean, usable_as_vehicle_key=True)
    elif clean and masked:
        # 2a) 遮挡 VIN：骨架 + 长度入键，弱键（同骨架可聚，不可自动并车）
        identity.update(
            kind="partial_masked",
            key="vinpart:m" + str(len(clean)) + ":" + _hash12(_masked_skeleton(clean)),
            check_error=None,
        )
    elif clean and len(clean) < VIN_LEN:
        # 2b) 截断/只露出一段：片段内容 + 长度入键
        identity.update(
            kind="partial_truncated",
            key="vinpart:t" + str(len(clean)) + ":" + _hash12(clean),
        )
    elif clean and len(clean) == VIN_LEN:
        # 3) 17 位但非法字符或校验失败：坏 VIN 聚簇键，不可作车辆键
        illegal = sorted({c for c in clean if c not in _VIN_ALPHABET})
        identity.update(
            kind="invalid",
            key="vinbad:" + _hash12(clean),
            check_error=("illegal-chars:" + "".join(illegal)) if illegal else "check-digit-mismatch",
        )
    elif clean:
        # 3b) 长度异常（>17 位；<17 位已在上面按截断处理）
        identity.update(
            kind="invalid",
            key="vinbad:" + _hash12(str(len(clean)) + ":" + clean),
            check_error="length-mismatch:" + str(len(clean)),
        )
    # clean 为空 ⇒ 缺 VIN，走降级

    # 降级身份：任何 VIN 状态都同时计算一份可审计 fallback。
    # 补号桥接：坏号/遮挡/截断记录补齐为完整 VIN 后，fallback_key 保持不变，
    # 可凭它把新强键认出是原记录的延续；完整 VIN 记录也携带该 hint 以便对账。
    claims, canonical = resolve_field_claims(rec)
    identity["fallback"] = _fallback(rec, canonical)
    if identity["kind"] is None:
        identity["kind"] = "fallback"
        identity["key"] = identity["fallback"]["key"]
    identity["completion_hint"] = {
        "fallback_key": identity["fallback"]["key"],
        "note": ("fallback_key stays identical across VIN repair; "
                 "resubmit a full 17-char VIN to obtain a strong vin: key"),
    }

    # 里程观测：与身份分开存放，任何情况下都不进主键
    obs = []
    for fld in ("mileage", "odo"):
        val = rec.get(fld)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        try:
            miles = int(float(str(val).strip()))
        except (TypeError, ValueError):
            continue
        obs.append({"mileage": miles, "src": src, "ts": str(rec.get("ts", ""))})

    rec["vin_identity"] = identity
    rec["mileage_obs"] = obs
    rec["sources"] = [{"src": src, "ts": str(rec.get("ts", "")), "record_id": rec.get("id")}]
    rec["field_claims"] = claims
    return rec
