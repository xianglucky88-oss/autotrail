"""vin_id 阶段的单元不变量：规范化、校验、遮挡/部分 VIN、降级身份、里程与键隔离。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autotrail import vin_id, keyer  # noqa: E402

VALID = "1HGCM82633A004352"  # 标准校验位算法下的合法 17 位 VIN


# ---- 规范化与校验 ---------------------------------------------------------------

def test_normalize_case_separators_and_validate():
    clean, masked, check = vin_id.normalize_vin(" 1hg-cm826/33a.004352 ")
    assert clean == VALID
    assert masked is False
    assert check is True


def test_known_valid_and_tampered_vin():
    assert vin_id.validate_vin(VALID) is True
    tampered = VALID[:8] + ("0" if VALID[8] != "0" else "1") + VALID[9:]
    assert vin_id.validate_vin(tampered) is False
    # I/O/Q 为非法字符
    assert vin_id.validate_vin(VALID[:-1] + "Q") is False


def test_mask_markers_normalized_to_star():
    clean, masked, check = vin_id.normalize_vin("1HGCM826#3A?04*52")
    assert masked is True and check is None
    assert "*" in clean and len(clean) == 17


def test_truncated_and_missing_vin():
    assert vin_id.normalize_vin(VALID[:11]) == (VALID[:11], False, None)
    assert vin_id.normalize_vin("") == ("", False, None)
    assert vin_id.normalize_vin(None) == ("", False, None)
    assert vin_id.normalize_vin(12345) == ("", False, None)


# ---- 四类身份键 -----------------------------------------------------------------

def test_full_vin_is_strong_vehicle_key():
    out = vin_id.process({"src": "a", "vin": VALID, "mileage": 1000})
    ident = out["vin_identity"]
    assert ident["kind"] == "full"
    assert ident["key"] == "vin:" + VALID
    assert ident["usable_as_vehicle_key"] is True
    assert keyer.key_of(out) == "vin:" + VALID


def test_masked_vin_is_weak_key_and_deterministic():
    r1 = vin_id.process({"src": "a", "vin": "1HG-CM826*3**0*352"})
    r2 = vin_id.process({"src": "b", "vin": "1HG CM826#3##0#352"})
    assert r1["vin_identity"]["kind"] == "partial_masked"
    assert r1["vin_identity"]["key"].startswith("vinpart:")
    assert r1["vin_identity"]["usable_as_vehicle_key"] is False
    assert r1["vin_identity"]["key"] == r2["vin_identity"]["key"]


def test_truncated_vin_uses_length_prefixed_partial_key():
    out = vin_id.process({"src": "a", "vin": VALID[:11]})
    assert out["vin_identity"]["kind"] == "partial_truncated"
    assert out["vin_identity"]["key"].startswith("vinpart:t11:")


def test_invalid_checkdigit_clusters_but_is_not_vehicle_key():
    bad = VALID[:8] + ("0" if VALID[8] != "0" else "1") + VALID[9:]
    r1 = vin_id.process({"src": "a", "vin": bad, "plate": "X1"})
    r2 = vin_id.process({"src": "b", "vin": bad.lower(), "plate": "X1"})
    assert r1["vin_identity"]["kind"] == "invalid"
    assert r1["vin_identity"]["check_error"] == "check-digit-mismatch"
    assert r1["vin_identity"]["key"].startswith("vinbad:")
    assert r1["vin_identity"]["usable_as_vehicle_key"] is False
    assert r1["vin_identity"]["key"] == r2["vin_identity"]["key"]  # 同坏号聚簇


def test_illegal_chars_and_wrong_length_are_distinct_invalid_errors():
    illegal = vin_id.process({"src": "a", "vin": VALID[:-1] + "Q"})
    overlong = vin_id.process({"src": "a", "vin": VALID + "00"})
    assert illegal["vin_identity"]["check_error"] == "illegal-chars:Q"
    assert overlong["vin_identity"]["check_error"].startswith("length-mismatch:")
    assert illegal["vin_identity"]["key"] != overlong["vin_identity"]["key"]


def test_blank_vin_and_bad_mileage_degrade_safely():
    blank = vin_id.process({"src": "a", "vin": "   ", "plate": "P9", "mileage": "n/a"})
    assert blank["vin_identity"]["kind"] == "fallback"
    assert blank["mileage_obs"] == []  # 无法解析的里程被跳过而非抛错
    numeric_string = vin_id.process({"src": "a", "vin": VALID, "mileage": "12300"})
    assert numeric_string["mileage_obs"][0]["mileage"] == 12300


def test_missing_vin_falls_back_with_audit_basis():
    out = vin_id.process({"src": "a", "plate": "沪A12345", "mileage": 99})
    ident = out["vin_identity"]
    assert ident["kind"] == "fallback"
    assert ident["key"].startswith("fallback:")
    assert ident["fallback"]["basis_fields"] == ["plate"]
    assert ident["fallback"]["cross_source_safe"] is False


def test_fallback_never_uses_mileage():
    r1 = vin_id.process({"src": "a", "plate": "P1", "mileage": 100})
    r2 = vin_id.process({"src": "a", "plate": "P1", "mileage": 99999})
    assert r1["vin_identity"]["key"] == r2["vin_identity"]["key"]


# ---- 里程与身份分离 --------------------------------------------------------------

def test_mileage_update_keeps_same_vehicle_key():
    r1 = vin_id.process({"src": "a", "vin": VALID, "mileage": 1000, "ts": "t1"})
    r2 = vin_id.process({"src": "a", "vin": VALID, "mileage": 2000, "ts": "t2"})
    assert keyer.key_of(r1) == keyer.key_of(r2) == "vin:" + VALID
    assert r1["mileage_obs"] == [{"mileage": 1000, "src": "a", "ts": "t1"}]
    assert r2["mileage_obs"][0]["mileage"] == 2000


def test_raw_vin_preserved_alongside_normalized_identity():
    raw = " 1hg-cm82633a004352 "
    out = vin_id.process({"src": "a", "vin": raw})
    assert out["vin"] == raw                       # 原始标识不动
    assert out["vin_identity"]["raw_vin"] == raw
    assert out["vin_identity"]["normalized_vin"] == VALID


# ---- 字段冲突裁定 ---------------------------------------------------------------

def test_field_conflict_resolution_is_deterministic_and_audited():
    older = vin_id.process({"src": "alpha", "vin": "1HG-CM826*3**0*352",
                            "color": "white", "ts": "2026-11-01T08:00:00Z"})
    newer = vin_id.process({"src": "beta", "vin": "1HG CM826#3##0#352",
                            "color": "pearl-white", "ts": "2026-11-05T09:30:00Z"})
    # 单条记录内：claims 留痕，原始字段保留
    assert older["field_claims"][0]["field"] == "color"
    assert older["color"] == "white"
    # 裁定规则确定性：同输入多次结果一致
    again = vin_id.process({"src": "beta", "vin": "1HG CM826#3##0#352",
                            "color": "pearl-white", "ts": "2026-11-05T09:30:00Z"})
    assert again["field_claims"] == newer["field_claims"]


# ---- 老格式兼容 -----------------------------------------------------------------

def test_legacy_record_passes_through_untouched():
    legacy = {"id": "r-001", "src": "alpha", "name": " Record One ", "value": 10}
    out = vin_id.process(legacy)
    assert out == legacy
    assert "vin_identity" not in out and "mileage_obs" not in out
    assert keyer.key_of(out) == "r-001"           # 基线键行为不变


def test_process_does_not_mutate_input():
    rec = {"src": "a", "vin": VALID, "mileage": 1}
    snapshot = dict(rec)
    vin_id.process(rec)
    assert rec == snapshot


if __name__ == "__main__":  # 支持脱离 pytest 直接运行
    raise SystemExit(pytest.main([__file__, "-q"]))
