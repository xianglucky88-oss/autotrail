"""单一职责：完整 VIN（车辆识别代号）的解析与标识校验。

本模块只负责“号码本身”的判定：
- 规范化（去空白、大写）；
- 判定 full / partial / absent / invalid；
- 对完整 17 位 VIN 做 ISO 3779 校验位核验。

身份归并（候选身份、别名、置信度）属于 identity 阶段，
本模块不做跨记录判断，仅把校验结果挂到 ``_vin`` 字段供下游使用。
"""

VIN_LEN = 17
_FORBIDDEN = frozenset("IOQ")  # 标准 VIN 字母表不含 I/O/Q
_TRANS = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
    "7": 7, "8": 8, "9": 9, "A": 1, "B": 2, "C": 3, "D": 4,
    "E": 5, "F": 6, "G": 7, "H": 8, "J": 1, "K": 2, "L": 3,
    "M": 4, "N": 5, "P": 7, "R": 9, "S": 2, "T": 3, "U": 4,
    "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)

FULL = "full"
PARTIAL = "partial"
ABSENT = "absent"
INVALID = "invalid"


def normalize_vin(raw):
    """规范化原始 VIN 文本；空值返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip().upper()
    return text or None


def expected_check_digit(vin):
    """计算 17 位 VIN 第 9 位应有的校验位（10 记为 X）。"""
    total = sum(_TRANS[ch] * w for ch, w in zip(vin, _WEIGHTS))
    rem = total % 11
    return "X" if rem == 10 else str(rem)


def _tail_fragment(text):
    """取脱敏 VIN 末尾连续字母数字片段，作为不完整号码的弱证据。"""
    tail = []
    for ch in reversed(text):
        if ch.isalnum():
            tail.append(ch)
        else:
            break
    frag = "".join(reversed(tail))
    return frag if len(frag) >= 4 else None


def classify_vin(raw):
    """返回结构化 VIN 校验结果，不依赖任何跨记录状态。"""
    text = normalize_vin(raw)
    if text is None:
        return {"status": ABSENT, "vin": None, "fragment": None, "check": "none"}

    if len(text) == VIN_LEN and all(ch.isalnum() for ch in text):
        if any(ch in _FORBIDDEN for ch in text):
            return {"status": INVALID, "vin": None,
                    "fragment": _tail_fragment(text), "check": "bad_alphabet"}
        check = "ok" if expected_check_digit(text) == text[8] else "bad"
        return {"status": FULL, "vin": text, "fragment": text[-6:], "check": check}

    # 含脱敏符（*、x 掩码）或长度不足：不完整 VIN，保留可辨识尾号
    return {"status": PARTIAL, "vin": None,
            "fragment": _tail_fragment(text), "check": "none"}


def process(record):
    """流水线阶段：在记录副本上挂 ``_vin`` 校验结果，不改动其他字段。"""
    rec = dict(record)
    rec["_vin"] = classify_vin(rec.get("vin"))
    return rec
