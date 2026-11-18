"""去重决策测试。

交错输入：重传、编辑、撤销合并、真正新增按任意顺序喂入；
覆盖冲突字段、同车跨门店、同门店重新挂牌、字段缺失、相似但不同车辆、
历史记录回放；验证旧结果兼容、撤销后来源可恢复、比较量随候选规模增长。
"""
import pytest

from src.autotrail import store, pipeline
from src.autotrail.journal import Session
from src.autotrail.keyer import source_key_of


# ------------------------------------------------------------------ 夹具
def _listing(id, src, **kw):
    base = {
        "id": id, "src": src, "name": id, "value": 0,
        "vin": None, "mileage": None, "model": None,
        "address": None, "price": None, "status": "active",
        "ts": "2026-11-10T00:00:00Z",
    }
    base.update(kw)
    return base


@pytest.fixture()
def session():
    con = store.connect(":memory:")
    ctx = Session(con)
    ctx.reset_counters()
    yield ctx, con
    con.close()


def feed(ctx, con, raw):
    return pipeline.ingest_record(raw, ctx, con)


def outcome(rec):
    return rec["_dedup"]["outcome"], rec["_dedup"]["reason"]


# ---------------------------------------------------------- 1) 旧结果兼容
def test_legacy_pipeline_compat_and_idempotent(tmp_path):
    db = tmp_path / "legacy.db"
    con = store.connect(db)
    a = pipeline.run(con)
    b = pipeline.run(con)  # 整轮重放
    assert a["fed"] == 4
    assert b["rows"] == a["rows"] == 3          # r-003 重传仍去重为一行
    assert b["comparisons"] >= a["comparisons"]
    keys = {k for k, _ in store.all_records(con)}
    assert keys == {"r-001", "r-002", "r-003"}  # 稳定键不漂移
    con.close()


# ---------------------------------------------------------- 2) 完全重传
def test_retransmit_merges_without_new_version(session):
    ctx, con = session
    r1 = feed(ctx, con, _listing("L1", "alpha", vin="VIN-A", mileage=10000,
                                 model="Model X", address="Shop 1", price=50))
    r1b = feed(ctx, con, _listing("L1", "alpha", vin="VIN-A", mileage=10000,
                                  model="Model X", address="Shop 1", price=50))
    assert outcome(r1) == ("keep", "new_listing")
    assert outcome(r1b) == ("merge", "retransmit_same_content")
    assert r1b["_source_seq"] == 1              # 重传不追加来源版本
    assert r1b["_decision_id"] == r1["_decision_id"]  # 决策 ID 稳定
    assert store.count(con) == 1


# ---------------------------------------------------------- 3) 编辑（价格/状态）
def test_edit_price_change_is_merge_with_diff_evidence(session):
    ctx, con = session
    feed(ctx, con, _listing("L1", "alpha", vin="VIN-A", mileage=10000,
                            model="Model X", address="Shop 1", price=50))
    edited = feed(ctx, con, _listing(
        "L1", "alpha", vin="VIN-A", mileage=10000,
        model="Model X", address="Shop 1", price=45,
        ts="2026-11-11T00:00:00Z"))
    assert outcome(edited) == ("merge", "edit_same_source")
    changes = edited["_event"]["changes"]
    assert set(changes) == {"price", "ts"}
    # 证据里必须留下价格变化，而不是静默覆盖。
    price_ev = [e for e in edited["_dedup"]["evidence"]
                if e.get("field") == "price"]
    assert price_ev and price_ev[0]["verdict"] == "change"
    assert edited["_source_seq"] == 2          # 编辑追加新版本，旧版本可追溯
    assert store.get_source(con, "L1", seq=1)["price"] == 50


# ---------------------------------------------------------- 4) 同 VIN 里程冲突：保守 vs 积极
def test_same_vin_conflicting_mileage_conservative_review(session):
    ctx, con = session
    feed(ctx, con, _listing("L1", "alpha", vin="VIN-C", mileage=10000,
                            model="Model X", address="Shop 1"))
    other = feed(ctx, con, _listing(
        "L2", "beta", vin="VIN-C", mileage=20000,
        model="Model X", address="Shop 1",
        ts="2026-11-12T00:00:00Z"))
    assert outcome(other) == ("review", "mileage_conflict_cross_store")
    # 待审不自动并入：两来源分组相互独立。
    assert ctx.group_of("L1") == "L1"
    assert ctx.group_of("L2") == "L2"
    note = other["_dedup"]["policy"]["mileage_conflict_cost"]
    assert "保守拒绝" in note and "分裂" in note  # 代价可解释
    ev_fields = {e["field"]: e["verdict"]
                 for e in other["_dedup"]["evidence"] if "field" in e}
    assert ev_fields["mileage"] == "conflict"
    assert ev_fields["vin"] == "match"


def test_same_vin_conflicting_mileage_aggressive_merge(tmp_path):
    con = store.connect(":memory:")
    ctx = Session(con, policy={"mileage_conflict": "merge"})
    feed(ctx, con, _listing("L1", "alpha", vin="VIN-C", mileage=10000,
                            model="Model X", address="Shop 1"))
    other = feed(ctx, con, _listing(
        "L2", "beta", vin="VIN-C", mileage=20000,
        model="Model X", address="Shop 1",
        ts="2026-11-12T00:00:00Z"))
    assert other["_dedup"]["outcome"] == "merge"
    assert ctx.group_of("L2") == ctx.group_of("L1") == "L1"
    note = other["_dedup"]["policy"]["mileage_conflict_cost"]
    assert "积极合并" in note and "误录" in note  # 代价同样可解释
    con.close()


def test_pending_review_group_blocks_auto_merge(session):
    ctx, con = session
    feed(ctx, con, _listing("C1", "alpha", vin="VIN-C", mileage=10000,
                            model="Model X", address="Shop 1"))
    c2 = feed(ctx, con, _listing(
        "C2", "beta", vin="VIN-C", mileage=20000,
        model="Model X", address="Shop 1",
        ts="2026-11-12T00:00:00Z"))
    assert c2["_dedup"]["outcome"] == "review"
    # 第三条同 VIN 证据：待审的 C2 不接受自动并入，C3 归入正常的 C1 分组。
    c3 = feed(ctx, con, _listing(
        "C3", "gamma", vin="VIN-C", mileage=10000,
        model="Model X", address="Shop 1",
        ts="2026-11-13T00:00:00Z"))
    assert outcome(c3) == ("merge", "cross_store_repost")
    assert ctx.group_of("C2") == "C2"          # 待审来源保持独立
    assert ctx.group_of("C3") == "C1"


# ---------------------------------------------------------- 5) 同车跨门店转载
def test_cross_store_repost_merges_with_lineage_evidence(session):
    ctx, con = session
    feed(ctx, con, _listing("L1", "alpha", vin="VIN-X", mileage=10000,
                            model="Model S", address="Shop 1", price=80))
    repost = feed(ctx, con, _listing(
        "L9", "beta", vin="VIN-X", mileage=10000,
        model="Model S", address="Shop 2", price=75,
        ts="2026-11-13T00:00:00Z"))
    assert outcome(repost) == ("merge", "cross_store_repost")
    assert repost["_group"] == "L1"
    note = [e for e in repost["_dedup"]["evidence"]
            if e.get("type") == "store_lineage"][0]
    assert note["cross_store"] is True
    assert note["candidate_source"] == "L1"
    # 跨门店转载不覆盖原始来源，两条来源版本都还在账本里。
    assert store.get_source(con, "L1") is not None
    assert store.get_source(con, "L9") is not None


# ---------------------------------------------------------- 6) 同门店重新挂牌
def test_relist_after_sold_is_relist_merge(session):
    ctx, con = session
    feed(ctx, con, _listing("L1", "alpha", vin="VIN-R", mileage=10000,
                            model="Model 3", address="Shop 1",
                            status="sold", ts="2026-11-01T00:00:00Z"))
    relisted = feed(ctx, con, _listing(
        "L1", "alpha", vin="VIN-R", mileage=10000,
        model="Model 3", address="Shop 1", status="active",
        ts="2026-11-20T00:00:00Z"))
    assert relisted["_event"]["kind"] == "relist"
    assert outcome(relisted) == ("merge", "relist_same_store")


def test_new_active_listing_after_sold_new_id_is_relist_merge(session):
    ctx, con = session
    feed(ctx, con, _listing("L1", "alpha", vin="VIN-R2", mileage=10000,
                            model="Model 3", address="Shop 1",
                            status="sold", ts="2026-11-01T00:00:00Z"))
    relisted = feed(ctx, con, _listing(
        "L2", "alpha", vin="VIN-R2", mileage=10000,
        model="Model 3", address="Shop 1", status="active",
        ts="2026-11-20T00:00:00Z"))
    assert outcome(relisted) == ("merge", "relist_same_store")
    assert ctx.group_of("L2") == "L1"


# ---------------------------------------------------------- 7) 字段缺失
def test_missing_vin_weak_identity_goes_to_review(session):
    ctx, con = session
    weak = feed(ctx, con, _listing("W1", "alpha"))  # vin/model/address/里程全缺
    assert outcome(weak) == ("review", "insufficient_identity_fields")
    assert weak["_identity"]["confidence"] == "weak"


def test_missing_vin_but_composite_match_needs_review(session):
    ctx, con = session
    feed(ctx, con, _listing("C1", "alpha", mileage=30000,
                            model="Model Y", address="Shop 9"))
    similar = feed(ctx, con, _listing(
        "C2", "beta", mileage=30000, model="Model Y",
        address="Shop 9", ts="2026-11-14T00:00:00Z"))
    # 无 VIN 兜底的跨来源组合匹配：只敢待审，不自动合并。
    assert outcome(similar) == ("review", "composite_match_needs_review")
    assert ctx.group_of("C1") != ctx.group_of("C2")


def test_vin_missing_against_vin_candidate_is_review(session):
    ctx, con = session
    feed(ctx, con, _listing("V1", "alpha", vin="VIN-Z", mileage=10000,
                            model="Model X", address="Shop 1"))
    # 无 VIN 记录与候选落在同一里程桶，车型/地址/里程都吻合：
    # 但候选持有 VIN 而当前记录缺失，证据不对称，禁止自动并入。
    no_vin = feed(ctx, con, _listing(
        "V2", "alpha", vin=None, mileage=10000,
        model="Model X", address="Shop 1",
        ts="2026-11-14T00:00:00Z"))
    assert outcome(no_vin) == (
        "review", "vin_missing_against_vin_candidate")
    assert ctx.group_of("V1") != ctx.group_of("V2")


def test_vin_missing_in_separate_block_is_kept(session):
    ctx, con = session
    feed(ctx, con, _listing("V3", "alpha", vin="VIN-QQ", mileage=99999,
                            model="Model Q", address="Shop 7"))
    # 无 VIN 且里程段不同：进入不同候选桶，没有可比候选，按新增保留。
    no_vin = feed(ctx, con, _listing(
        "V4", "alpha", vin=None, mileage=10000,
        model="Model X", address="Shop 1",
        ts="2026-11-14T00:00:00Z"))
    assert outcome(no_vin) == ("keep", "new_listing")


# ---------------------------------------------------------- 8) 相似但不同车辆
def test_similar_but_distinct_vehicles_kept_separate(session):
    ctx, con = session
    feed(ctx, con, _listing("D1", "alpha", mileage=30000,
                            model="Model Y", address="Shop 9", price=60))
    # 同里程段、价格相近，但车型与地址都不同。
    other = feed(ctx, con, _listing(
        "D2", "alpha", mileage=30100, model="Model Z",
        address="Shop 10", price=61, ts="2026-11-15T00:00:00Z"))
    assert outcome(other) == ("keep", "similar_but_distinct_vehicle")
    assert ctx.group_of("D1") == "D1" and ctx.group_of("D2") == "D2"


def test_same_vin_hard_field_conflict_kept_separate(session):
    ctx, con = session
    feed(ctx, con, _listing("H1", "alpha", vin="VIN-H", mileage=10000,
                            model="Model X", address="Shop 1"))
    # 同 VIN 但车型冲突：极可能是 VIN 误录，保守保留为两辆车。
    other = feed(ctx, con, _listing(
        "H2", "beta", vin="VIN-H", mileage=10000,
        model="Model Z", address="Shop 1",
        ts="2026-11-15T00:00:00Z"))
    assert outcome(other) == ("keep", "same_vin_hard_field_conflict")
    assert ctx.group_of("H1") != ctx.group_of("H2")


# ---------------------------------------------------------- 9) 历史记录回放
def test_historical_replay_does_not_change_groups(session):
    ctx, con = session
    feed(ctx, con, _listing("P1", "alpha", vin="VIN-P", mileage=10000,
                            model="Model X", address="Shop 1", price=50,
                            ts="2026-11-10T00:00:00Z"))
    feed(ctx, con, _listing("P1", "alpha", vin="VIN-P", mileage=11000,
                            model="Model X", address="Shop 1", price=48,
                            ts="2026-11-12T00:00:00Z"))  # 编辑
    replay = feed(ctx, con, _listing(
        "P1", "alpha", vin="VIN-P", mileage=10500,
        model="Model X", address="Shop 1", price=49,
        ts="2026-11-11T00:00:00Z"))  # 时间戳更早 -> 回放
    assert replay["_event"]["kind"] == "replay"
    assert outcome(replay) == ("keep", "historical_replay")
    # 回放不改写最新版本，当前仍为 11-12 的编辑结果。
    assert store.get_source(con, "P1")["mileage"] == 11000
    # records 表是当前版本视图，回放记录不覆盖已落库的最新挂牌。
    rows = dict(store.all_records(con))
    assert rows["P1"]["mileage"] == 11000
    # 但回放版本本身在只追加账本中完整留痕，可随时追溯。
    replay_seq = replay["_source_seq"]
    assert store.get_source(con, "P1", seq=replay_seq)["mileage"] == 10500


# ---------------------------------------------------------- 10) 撤销合并后来源可恢复
def test_undo_merge_restores_source_and_is_stable(session):
    ctx, con = session
    feed(ctx, con, _listing("U1", "alpha", vin="VIN-U", mileage=10000,
                            model="Model X", address="Shop 1"))
    repost = feed(ctx, con, _listing(
        "U2", "beta", vin="VIN-U", mileage=10000,
        model="Model X", address="Shop 2",
        ts="2026-11-16T00:00:00Z"))
    assert outcome(repost) == ("merge", "cross_store_repost")
    assert ctx.group_of("U2") == "U1"

    merge_dec = ctx.decisions_for_source("U2")[-1]
    undo = ctx.undo_merge(merge_dec, "2026-11-17T00:00:00Z")
    store.commit(con)

    assert undo["outcome"] == "undo_merge"
    assert ctx.group_of("U2") == "U2"          # 来源身份恢复
    assert ctx.group_of("U1") == "U1"
    marked = ctx.decision_for(merge_dec["decision_id"])
    assert marked["undone_by"] == undo["decision_id"]
    # 原始来源数据从未被覆盖，撤销后可完整恢复。
    assert store.get_source(con, "U2")["vin"] == "VIN-U"
    # 撤销本身可重放：重复撤销返回同一条反向决策。
    again = ctx.undo_merge(merge_dec, "2026-11-17T00:00:00Z")
    assert again["decision_id"] == undo["decision_id"]


# ---------------------------------------------------------- 11) 交错输入：重传/编辑/新增混排
def test_interleaved_inputs_are_stable(session):
    ctx, con = session
    feed(ctx, con, _listing("I1", "alpha", vin="VIN-I", mileage=10000,
                            model="Model X", address="Shop 1"))
    feed(ctx, con, _listing("I2", "gamma", vin="VIN-NEW", mileage=5000,
                            model="Model 3", address="Shop 5"))  # 真正新增
    edit = feed(ctx, con, _listing(
        "I1", "alpha", vin="VIN-I", mileage=10000,
        model="Model X", address="Shop 1", price=44,
        ts="2026-11-18T00:00:00Z"))                              # 编辑
    dup = feed(ctx, con, _listing(
        "I2", "gamma", vin="VIN-NEW", mileage=5000,
        model="Model 3", address="Shop 5"))                     # 重传
    assert outcome(edit) == ("merge", "edit_same_source")
    assert outcome(dup) == ("merge", "retransmit_same_content")
    assert ctx.group_of("I1") == "I1" and ctx.group_of("I2") == "I2"
    assert store.count(con) == 2


# ---------------------------------------------------------- 12) 比较量随候选规模记录
def test_comparison_count_grows_with_candidate_block_size(session):
    ctx, con = session
    # 同一 VIN 桶内逐步加入同车转载（每次都与桶内候选比较）。
    feed(ctx, con, _listing("G1", "alpha", vin="VIN-G", mileage=10000,
                            model="Model X", address="Shop 1"))
    sizes = []
    for i in range(2, 7):
        rec = feed(ctx, con, _listing(
            f"G{i}", "beta", vin="VIN-G", mileage=10000,
            model="Model X", address=f"Shop {i}",
            ts=f"2026-11-{10+i:02d}T00:00:00Z"))
        assert rec["_dedup"]["outcome"] == "merge"
        sizes.append(ctx.block_sizes[rec["_identity"]["block_key"]])
    assert sizes == [2, 3, 4, 5, 6]            # 桶规模被记录
    # 每条新记录与桶内全部既有候选做 6 字段比较：总量可解释、有上界。
    assert ctx.comparisons == sum(range(1, 6)) * 6
    # 无关 VIN 独立成桶，不参与该桶比较（避免全量历史扫描）。
    feed(ctx, con, _listing("Z1", "alpha", vin="VIN-OTHER", mileage=9000,
                            model="Model X", address="Shop 1"))
    assert ctx.block_sizes["blk:vin:VIN-OTHER"] == 1


# ---------------------------------------------------------- 13) 决策带依据并随 keyer 落库
def test_decisions_persisted_with_evidence(session):
    ctx, con = session
    rec = feed(ctx, con, _listing("E1", "alpha", vin="VIN-E", mileage=10000,
                                  model="Model X", address="Shop 1", price=50))
    store.commit(con)
    dec = ctx.decisions_for_source("E1")[0]
    assert dec["outcome"] == "keep" and dec["reason"] == "new_listing"
    assert dec["evidence"] and dec["policy"]
    # records 表中的数据也带决策标注与稳定键。
    rows = dict(store.all_records(con))
    assert "E1" in rows
    assert rows["E1"]["_decision_id"] == rec["_decision_id"]
    assert rows["E1"]["_group"] == rec["_group"]
