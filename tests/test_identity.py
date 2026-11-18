"""身份归并测试：无完整 VIN 挂牌的证据加权、待审、回放冻结与乱序对照。

运行：PYTHONPATH=src python3 -m pytest -q（无 pytest 时也可直接执行本文件）。
"""
import json
from collections import defaultdict
from pathlib import Path

try:
    import pytest
except ImportError:  # 环境无 pytest 时，文件底部的内置运行器仍可执行
    class _Mark:
        @staticmethod
        def parametrize(*_args, **_kwargs):
            def decorator(fn):
                return fn
            return decorator

    class pytest:  # noqa: N801 - 最小占位
        mark = _Mark()

from src.autotrail import identity, normalizer, pipeline, vin_id
from src.autotrail import statusline, dedup
from src.autotrail.keyer import key_of

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "identity_cases.json"


# --------------------------------------------------------------------------
# 测试辅助
# --------------------------------------------------------------------------
def _load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _prep(raw):
    return vin_id.process(normalizer.process(dict(raw)))


def _scenarios():
    grouped = defaultdict(list)
    for rec in _load_cases():
        grouped[rec["_scenario"]].append(rec)
    return grouped


def _batch(records):
    reg = identity.IdentityRegistry()
    result = identity.resolve_batch([_prep(r) for r in records], registry=reg)
    return reg, result


# --------------------------------------------------------------------------
# vin_id：完整号码解析仍是 vin_id 的职责
# --------------------------------------------------------------------------
class TestVinId:
    def test_full_vin_with_valid_check_digit(self):
        info = vin_id.classify_vin("LFV2A21K9ME500001")
        assert info["status"] == vin_id.FULL
        assert info["vin"] == "LFV2A21K9ME500001"
        assert info["check"] == "ok"
        assert info["fragment"] == "500001"

    def test_full_vin_bad_check_digit_still_identified(self):
        info = vin_id.classify_vin("LFV2A21K8ME500001")
        assert info["status"] == vin_id.FULL
        assert info["check"] == "bad"  # 号码可识别，但校验位异常可被下游感知

    def test_forbidden_letters_are_invalid(self):
        assert vin_id.classify_vin("IOQ0A21K8ME500001")["status"] == vin_id.INVALID

    def test_masked_vin_is_partial_with_tail_fragment(self):
        info = vin_id.classify_vin("*******ME500001")
        assert info["status"] == vin_id.PARTIAL
        assert info["vin"] is None
        assert info["fragment"] == "ME500001"

    def test_absent_vin(self):
        assert vin_id.classify_vin(None)["status"] == vin_id.ABSENT
        assert vin_id.classify_vin("  ")["status"] == vin_id.ABSENT

    def test_process_attaches_vin_block_only(self):
        out = vin_id.process({"id": "x", "vin": "LFV2A21K9ME500001"})
        assert out["_vin"]["status"] == vin_id.FULL
        assert out["id"] == "x"  # 其他字段保持不动


# --------------------------------------------------------------------------
# 证据加权与阈值
# --------------------------------------------------------------------------
class TestScoring:
    def _ident(self, raw):
        holder = identity.VehicleIdentity(raw["id"])
        holder.absorb(identity.evidence_profile(_prep(raw)))
        return holder

    def test_same_plate_and_vehicle_traits_scores_above_confirm(self):
        cases = _scenarios()["relocate_full"]
        holder = self._ident(cases[0])
        info = identity.score_against(holder,
                                      identity.evidence_profile(_prep(cases[1])))
        assert info["score"] >= identity.AUTO_CONFIRM_SCORE
        assert info["conflicts"] == []
        assert any(f["factor"] == "plate" and f["result"] == "match"
                   for f in info["factors"])

    def test_model_alone_is_below_pending_threshold(self):
        # 仅品牌车型一致不足以并车（无车牌/VIN/门店等其他证据）
        a = {"id": "a", "make": "丰田", "model": "凯美瑞", "year": 2022,
             "trim": "X", "mileage": 1000, "price": 100000}
        b = {"id": "b", "make": "丰田", "model": "凯美瑞", "year": 2022,
             "trim": "Y", "mileage": 90000, "price": 50000}
        holder = self._ident(a)
        info = identity.score_against(holder, identity.evidence_profile(_prep(b)))
        assert info["score"] < identity.PENDING_SCORE

    def test_year_mismatch_is_veto(self):
        cases = _scenarios()["conflicts"]
        holder = self._ident(cases[0])           # cf-001, 2021
        info = identity.score_against(holder,
                                      identity.evidence_profile(_prep(cases[2])))
        assert info["veto"] is True
        assert "year_mismatch" in info["conflicts"]
        assert info["score"] == 0.0

    def test_full_vin_mismatch_is_veto(self):
        a = _prep({"id": "a", "vin": "LFV2A21K9ME500001"})
        b = _prep({"id": "b", "vin": "LFV2A21K0ME500002"})
        holder = self._ident(a)
        info = identity.score_against(holder, identity.evidence_profile(b))
        assert info["veto"] is True
        assert "full_vin_mismatch" in info["conflicts"]


# --------------------------------------------------------------------------
# 批处理归并：各业务场景的预期归属
# --------------------------------------------------------------------------
class TestBatchResolve:
    def test_relocate_full_confirms_single_identity_across_stores(self):
        reg, result = _batch(_scenarios()["relocate_full"])
        ids = {result[k]["identity_id"] for k in ("rl-001", "rl-002")}
        assert len(ids) == 1
        assert all(result[k]["decision"] == identity.CONFIRMED
                   for k in ("rl-001", "rl-002"))
        assert reg.reviews() == []

    def test_relocate_missing_fields_can_only_be_pending(self):
        reg, result = _batch(_scenarios()["relocate_missing"])
        # 缺车牌/VIN：证据不足自动确认，两条都进入待审，不静默并车也不丢弃
        assert result["rm-001"]["decision"] == identity.PENDING
        assert result["rm-002"]["decision"] == identity.PENDING
        assert (result["rm-001"]["identity_id"]
                != result["rm-002"]["identity_id"])
        assert len(reg.reviews()) == 1
        assert reg.reviews()[0]["reason"] == "below_threshold"

    def test_identically_configured_cars_are_never_merged(self):
        reg, result = _batch(_scenarios()["twin_full"])
        assert result["tw-001"]["decision"] == identity.NEW
        assert result["tw-002"]["decision"] == identity.NEW
        assert (result["tw-001"]["identity_id"]
                != result["tw-002"]["identity_id"])
        # 车牌与 VIN 尾号双双对不上：连待审噪音都不产生
        assert reg.reviews() == []

    def test_identically_configured_cars_without_plates_stay_pending(self):
        reg, result = _batch(_scenarios()["twin_missing"])
        assert result["tm-001"]["decision"] == identity.PENDING
        assert result["tm-002"]["decision"] == identity.PENDING
        assert (result["tm-001"]["identity_id"]
                != result["tm-002"]["identity_id"])
        assert len(reg.reviews()) == 1

    def test_field_conflicts_are_pending_or_vetoed(self):
        reg, result = _batch(_scenarios()["conflicts"])
        # 同年同牌但配置冲突：待审
        assert result["cf-001"]["decision"] == identity.PENDING
        assert result["cf-002"]["decision"] == identity.PENDING
        # 年份硬冲突：另立身份，不允许自动归并
        assert result["cf-003"]["decision"] == identity.PENDING  # 有车牌交集，挂待审
        assert (result["cf-003"]["identity_id"]
                != result["cf-001"]["identity_id"])
        reasons = {r["reason"] for r in reg.reviews()}
        assert "hard_conflict" in reasons

    def test_transfer_relisting_requires_manual_review(self):
        reg, result = _batch(_scenarios()["transfer"])
        assert result["tf-001"]["decision"] == identity.PENDING
        assert result["tf-002"]["decision"] == identity.PENDING
        assert (result["tf-001"]["identity_id"]
                != result["tf-002"]["identity_id"])
        review = reg.reviews()[0]
        assert review["conflicts"] == ["mileage_mismatch"]

    def test_full_vin_channel_merges_same_number_across_stores(self):
        reg, result = _batch(_scenarios()["vin_channel"])
        assert result["vc-001"]["identity_id"] == result["vc-002"]["identity_id"]
        assert result["vc-001"]["decision"] == identity.CONFIRMED
        # 同 VIN 但年份硬冲突：另立身份 + 待审，归属不被改写
        assert (result["vc-003"]["identity_id"]
                != result["vc-001"]["identity_id"])
        assert any(r["reason"] == "hard_conflict" for r in reg.reviews())

    def test_confidence_is_explainable(self):
        _, result = _batch(_scenarios()["relocate_full"])
        factors = result["rl-002"]["factors"]
        names = {f["factor"] for f in factors}
        assert {"plate", "make_model", "year", "trim"} <= names
        assert all("weight" in f and "result" in f and "detail" in f
                   for f in factors)


# --------------------------------------------------------------------------
# 顺序不变性：来源到达顺序颠倒后，分组/结论/待审完全一致
# --------------------------------------------------------------------------
class TestOrderInvariance:
    @pytest.mark.parametrize("scenario", [
        "relocate_full", "relocate_missing", "twin_full", "twin_missing",
        "conflicts", "transfer", "vin_channel", "freeze_guard",
    ])
    def test_reversed_arrival_order_keeps_assignment(self, scenario):
        recs = _scenarios()[scenario]

        def snapshot(seq):
            reg, result = _batch(seq)
            decisions = {k: (v["decision"], v["identity_id"])
                         for k, v in result.items()}
            groups = sorted(
                (i["status"], i["identity_id"],
                 tuple(sorted(i["aliases"]["listings"])))
                for i in reg.identities())
            reviews = sorted(
                (r["review_id"], tuple(r.get("identity_ids", [])), r["reason"])
                for r in reg.reviews())
            return decisions, groups, reviews

        forward = snapshot(recs)
        reversed_ = snapshot(list(reversed(recs)))
        assert forward == reversed_

    def test_identity_id_is_anchor_derived_and_stable(self):
        reg, result = _batch(_scenarios()["relocate_full"])
        ident = reg.identities()[0]
        # 锚点是最小成员键，ID 由其确定性派生
        assert ident["anchor_key"] == "rl-001"
        assert ident["identity_id"] == result["rl-001"]["identity_id"]


# --------------------------------------------------------------------------
# 回放冻结：新增线索不会无声改写已确认归属
# --------------------------------------------------------------------------
class TestReplayFreeze:
    def test_streaming_confirmed_identity_rejects_silent_rewrite(self):
        reg = identity.IdentityRegistry()
        base = _prep({
            "id": "s-1", "src": "alpha", "vin": "*******ME110001",
            "plate": "沪F·**210", "make": "奔驰", "model": "C级",
            "year": 2021, "trim": "C260L", "mileage": 33000, "price": 242000,
        })
        second = _prep(dict(base, id="s-2", src="beta", mileage=33400))
        identity.process(base, registry=reg)
        out2 = identity.process(second, registry=reg)
        confirmed_id = out2["_identity"]["identity_id"]
        assert out2["_identity"]["decision"] == identity.CONFIRMED

        # 第三条强线索到达：已确认身份冻结，只能待审
        third = _prep(dict(base, id="s-3", src="gamma", mileage=33900,
                           price=240000))
        out3 = identity.process(third, registry=reg)
        assert out3["_identity"]["decision"] == identity.PENDING
        assert out3["_identity"]["reason"] == "confirmed_freeze_new_clue"
        ident = next(i for i in reg.identities()
                     if i["identity_id"] == confirmed_id)
        assert "s-3" not in ident["aliases"]["listings"]  # 归属未被改写
        assert any(r["reason"] == "confirmed_freeze_new_clue"
                   for r in reg.reviews())

    def test_replay_same_listing_is_idempotent(self):
        reg = identity.IdentityRegistry()
        rec = _prep({"id": "p-1", "make": "大众", "model": "迈腾"})
        first = identity.process(rec, registry=reg)["_identity"]
        second = identity.process(dict(rec), registry=reg)["_identity"]
        assert second["replayed"] is True
        assert second["identity_id"] == first["identity_id"]
        assert len(reg.identities()) == 1
        assert len(reg.reviews()) == 0

    def test_full_vin_conflict_opens_review_without_rewrite(self):
        reg = identity.IdentityRegistry()
        a = _prep({"id": "v-1", "vin": "LFV2A21K9ME500001",
                   "make": "奥迪", "model": "A4L", "year": 2023})
        b = _prep({"id": "v-2", "vin": "LFV2A21K9ME500001",
                   "make": "奥迪", "model": "A4L", "year": 2023})
        c = _prep({"id": "v-3", "vin": "LFV2A21K9ME500001",
                   "make": "奥迪", "model": "A4L", "year": 2022})
        identity.process(a, registry=reg)
        identity.process(b, registry=reg)
        out = identity.process(c, registry=reg)
        assert out["_identity"]["decision"] == identity.PENDING
        assert out["_identity"]["reason"] == "full_vin_conflict"
        ident = reg.identities()[0]
        assert "v-3" not in ident["aliases"]["listings"]


# --------------------------------------------------------------------------
# 人工裁决
# --------------------------------------------------------------------------
class TestManualReview:
    def test_approve_then_reject_flow(self):
        reg = identity.IdentityRegistry()
        a = _prep({
            "id": "m-1", "plate": "沪D·**007", "make": "宝马", "model": "3系",
            "year": 2020, "trim": "325Li", "mileage": 38000, "price": 235000,
        })
        b = _prep(dict(a, id="m-2", mileage=47000, price=218000))
        identity.process(a, registry=reg)
        out = identity.process(b, registry=reg)
        assert out["_identity"]["decision"] == identity.PENDING
        review_id = reg.reviews()[0]["review_id"]

        approved = reg.resolve_review(review_id, approve=True, note="过户确认")
        assert approved["state"] == "approved"
        assert reg.identity_of("m-2") == reg.identity_of("m-1")
        assert reg.decisions()[0]["approved"] is True

    def test_reject_spawns_independent_identity(self):
        reg = identity.IdentityRegistry()
        a = _prep({
            "id": "m-3", "plate": "沪D·**008", "make": "宝马", "model": "3系",
            "year": 2020, "trim": "325Li", "mileage": 38000, "price": 235000,
        })
        b = _prep(dict(a, id="m-4", mileage=70000, price=180000))
        identity.process(a, registry=reg)
        out = identity.process(b, registry=reg)
        review_id = reg.reviews()[0]["review_id"]
        rejected = reg.resolve_review(review_id, approve=False)
        assert rejected["state"] == "rejected"
        assert reg.identity_of("m-4") != reg.identity_of("m-3")
        assert reg.identity_of("m-3") == rejected["candidate_identity_id"]

    def test_batch_review_can_be_resolved(self):
        reg, _ = _batch(_scenarios()["transfer"])
        review = next(r for r in reg.reviews() if r["kind"] == "link")
        approved = reg.resolve_review(review["review_id"], approve=True,
                                      note="过户凭证已核验")
        assert approved["state"] == "approved"
        assert reg.decisions()[0]["note"] == "过户凭证已核验"


# --------------------------------------------------------------------------
# 流水线集成：process() 调用与历史键读法保持不变
# --------------------------------------------------------------------------
class TestPipelineIntegration:
    def test_identity_block_flows_through_stages_and_key_unchanged(self):
        identity.reset_registry()
        rec = normalizer.process({
            "id": "e2e-1", "src": "alpha", "name": "listing",
            "vin": "*******ME500001", "plate": "沪A·**123",
            "make": "大众", "model": "迈腾", "year": 2021,
            "trim": "豪华型", "mileage": 42000, "price": 168000,
        })
        chain = [vin_id.process, identity.process,
                 statusline.process, dedup.process]
        for fn in chain:
            rec = fn(rec)
        assert rec["_identity"]["identity_id"].startswith("veh-")
        assert rec["_status_identity_id"] == rec["_identity"]["identity_id"]
        # keyer 仍按记录 id 取历史键，读取方式未变
        assert key_of(rec) == "e2e-1"

    def test_baseline_pipeline_still_idempotent(self):
        identity.reset_registry()
        a = pipeline.run()
        b = pipeline.run()
        assert a["fed"] == 4
        assert b["rows"] == a["rows"] == 3


if __name__ == "__main__":
    # 无 pytest 环境下的最小运行器
    import inspect
    failed = 0
    for cls_name, cls in sorted(globals().items()):
        if not (inspect.isclass(cls) and cls_name.startswith("Test")):
            continue
        for name, fn in sorted(vars(cls).items()):
            if not name.startswith("test_"):
                continue
            params = getattr(fn, "pytestmark", None)
            sig = inspect.signature(fn)
            if "scenario" in sig.parameters:
                for sc in ["relocate_full", "relocate_missing", "twin_full",
                           "twin_missing", "conflicts", "transfer",
                           "vin_channel", "freeze_guard"]:
                    try:
                        fn(cls(), sc)
                        print("ok  ", f"{cls_name}.{name}[{sc}]")
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        print("FAIL", f"{cls_name}.{name}[{sc}]", exc)
            else:
                try:
                    fn(cls())
                    print("ok  ", f"{cls_name}.{name}")
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    print("FAIL", f"{cls_name}.{name}", exc)
    print(f"\n{failed} failure(s)")
    raise SystemExit(1 if failed else 0)
