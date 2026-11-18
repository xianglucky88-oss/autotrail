"""单一职责：去重决策层。

输入：identity 阶段的身份信息与 statusline 阶段的事件信息，加上 Session
账本中的候选来源。输出三种裁决（写入 ``_dedup``），随 keyer 落库并带依据：

- ``merge``：当前来源并入既有车辆分组（重传/编辑/重新挂牌/跨店转载）；
- ``keep``：判定为不同车辆，独立保留（含“相似但不同车辆”）；
- ``review``：证据不足或字段冲突，保守待审，不自动合并；
- 另有账本层的 ``undo_merge`` 用于撤销合并。

候选不是直接覆盖历史挂牌：每次裁决都生成逐字段证据（match/change/
conflict/missing），决策只追加、不覆盖来源，撤销后来源可恢复。
"""
from .identity import IDENTITY_OUT
from .statusline import EVENT_OUT, is_active
from .keyer import source_key_of

DECISION_OUT = "_dedup"

# 参与逐字段比较的业务字段及证据类型。
FIELD_ORDER = ("vin", "mileage", "model", "address", "price", "status")

# 同 VIN 里程冲突时两种策略的代价说明，随决策证据落库，可向人工解释。
MILEAGE_POLICY_COST = {
    "review": (
        "保守拒绝自动合并：两条同 VIN 挂牌里程冲突，可能是里程数据错误或"
        "VIN 误录；代价是同一辆车会暂时分裂为两条挂牌，需要人工复核后才能归并。"
    ),
    "merge": (
        "积极合并：接受里程差异为里程表更新或录入误差；代价是若其中一条实为"
        "VIN 误录的不同车辆，会被错误并入同一分组，原始里程证据仍保留可追溯。"
    ),
}


def _evidence_entry(field, current, candidate, verdict):
    return {
        "field": field,
        "verdict": verdict,  # match / change / conflict / missing
        "current": current,
        "candidate": candidate,
    }


def compare_fields(curr, cand, conflicts):
    """逐字段比较，产出证据列表（字段级比较量由调用方记账）。

    conflicts 中的字段在两边都有值且不同时记为 conflict：同 VIN 时里程
    差异是冲突、车型差异始终是硬身份冲突；其余差异记为 change，任一侧
    缺值记为 missing，相同记为 match。
    """
    evidence = []
    for field in FIELD_ORDER:
        new = curr.get(field)
        old = cand.get(field)
        if new in (None, "") or old in (None, ""):
            verdict = "missing"
        elif new == old:
            verdict = "match"
        elif field in conflicts:
            verdict = "conflict"
        else:
            verdict = "change"
        evidence.append(_evidence_entry(field, new, old, verdict))
    return evidence


def _has(evidence, field, verdict):
    return any(e["field"] == field and e["verdict"] == verdict
               for e in evidence)


def _count_hard_identity(evidence):
    """硬身份字段冲突数：车型对不上即视为不同车辆。

    地址不属于硬身份——同车跨门店转载必然伴随地址变化，地址差异由
    store_lineage 证据单独记录。
    """
    return sum(1 for e in evidence
               if e["field"] == "model" and e["verdict"] == "conflict")


def decide(record, ctx):
    """纯决策函数：返回 outcome/reason/target_group/evidence/policy 等。"""
    identity = record[IDENTITY_OUT]
    event = record[EVENT_OUT]
    skey = source_key_of(record)
    policy_note = dict(ctx.policy)

    # 一条记录可能属于多个候选桶（主桶 + 同里程段 VIN 影子桶）；
    # 跨桶收集后按来源去重，比较量按实际比较的候选数累计。
    candidates = []
    seen_sources = set()
    for block in identity["block_keys"]:
        ctx.note_block_size(block)
        for cand in ctx.candidates(block, skey):
            if cand["source_key"] in seen_sources:
                continue
            seen_sources.add(cand["source_key"])
            candidates.append(cand)

    # 桶内每条候选做一次逐字段比较；比较量随桶规模线性增长。
    # 同 VIN 时里程差异是冲突信号；无 VIN 组合匹配时里程仅为普通变化。
    # 车型是硬身份：不同即冲突；地址差异是跨店线索，按普通变化留证。
    has_vin = identity["confidence"] == "vin"
    conflicts = frozenset(("mileage", "model")) if has_vin \
        else frozenset(("model",))
    compared = []
    for cand in candidates:
        ctx.count_comparison(len(FIELD_ORDER))
        evidence = compare_fields(record, cand["data"], conflicts=conflicts)
        compared.append((cand, evidence))

    kind = event["kind"]

    # 决策前索引产生的其他来源占位尚未落账，永不参与本次裁决；
    # 同来源占位保留给编辑/重挂/回放路径做血缘对照。
    compared = [(c, e) for c, e in compared if not c.get("inflight")
                or c["source_key"] == skey]

    # ------------------------------------------------ 1) 确定性快捷路径
    if kind == "retransmit":
        # 完全重传：稳定归并回原分组，不产生新车辆。
        cand = _same_source(compared, skey)
        target = cand["group"] if cand else skey
        return _result("merge", "retransmit_same_content", target,
                       _retransmit_evidence(event, cand), policy_note)

    if kind == "replay":
        # 历史记录回放：不新于已收录版本，保留历史证据，不改变当前分组。
        cand = _same_source(compared, skey)
        target = cand["group"] if cand else skey
        return _result("keep", "historical_replay", target,
                       _replay_evidence(event, cand), policy_note)

    # 编辑/重新挂牌/新增都需要先看同来源的既有版本。
    same = _same_source(compared, skey)

    if kind in ("edit", "relist") and same is not None:
        target = same["group"]
        mileage_is_conflict = has_vin  # 同 VIN 的里程差异才是冲突信号
        ev = list(event["changes"].items())
        evidence = [{
            "type": "event_change",
            "field": field,
            "verdict": "conflict" if field == "mileage" and mileage_is_conflict
            else "change",
            "old": diff["old"],
            "new": diff["new"],
        } for field, diff in ev]
        evidence.append({
            "type": "lineage",
            "same_source": skey,
            "event": kind,
            "target_group": target,
        })
        if _mileage_conflict_in_event(event):
            if ctx.policy["mileage_conflict"] == "review":
                policy_note["mileage_conflict_cost"] = \
                    MILEAGE_POLICY_COST["review"]
                return _result("review", "mileage_conflict_edit", target,
                               evidence, policy_note)
            policy_note["mileage_conflict_cost"] = \
                MILEAGE_POLICY_COST["merge"]
            return _result("merge", "mileage_conflict_edit_accepted",
                           target, evidence, policy_note)
        reason = ("relist_same_store" if kind == "relist"
                  else "edit_same_source")
        return _result("merge", reason, target, evidence, policy_note)

    # ------------------------------------------------ 2) 候选裁决
    # 待审分组不接受自动并入：新证据进来后仍保持待审，交由人工。
    viable = [(c, e) for c, e in compared if not c["pending"]]

    if identity["confidence"] == "vin":
        decision = _decide_vin(record, identity, viable, event, policy_note, ctx)
    else:
        decision = _decide_composite(record, identity, viable, event,
                                     policy_note, compared)

    if decision is not None:
        return decision

    # 没有可用候选：独立车辆；身份证据过弱时保守待审。
    if identity["confidence"] == "weak":
        return _result("review", "insufficient_identity_fields", skey,
                       _weak_evidence(identity, event), policy_note)
    return _result("keep", "new_listing", skey,
                   _new_listing_evidence(identity, event, candidates),
                   policy_note)


# ------------------------------------------------------------------ VIN 分支
def _decide_vin(record, identity, viable, event, policy_note, ctx):
    vin = identity["vin"]
    skey = source_key_of(record)
    for cand, evidence in viable:
        if cand["data"].get("vin") != vin:
            # 同桶不应出现异 VIN；显式留下“相似但不同车辆”的证据。
            continue
        if _has(evidence, "vin", "match") and _count_hard_identity(evidence):
            return _result(
                "keep", "same_vin_hard_field_conflict", skey,
                evidence + [_cross_note(record, cand)], policy_note)
        if _has(evidence, "mileage", "conflict"):
            cross_store = record.get("src") != cand["data"].get("src")
            if ctx.policy["mileage_conflict"] == "review":
                policy_note["mileage_conflict_cost"] = \
                    MILEAGE_POLICY_COST["review"]
                reason = ("mileage_conflict_cross_store" if cross_store
                          else "mileage_conflict")
                return _result("review", reason, cand["group"],
                               evidence + [_cross_note(record, cand)],
                               policy_note)
            policy_note["mileage_conflict_cost"] = \
                MILEAGE_POLICY_COST["merge"]
            reason = ("mileage_conflict_merge_cross_store" if cross_store
                      else "mileage_conflict_accepted")
            return _result("merge", reason, cand["group"],
                           evidence + [_cross_note(record, cand)],
                           policy_note)
        # 无冲突：允许价格/状态变化（非身份字段），按门店关系区分依据。
        cross_store = record.get("src") != cand["data"].get("src")
        reason = ("cross_store_repost" if cross_store
                  else "relist_same_store")
        return _result("merge", reason, cand["group"],
                       evidence + [_cross_note(record, cand)], policy_note)
    return None


# ------------------------------------------------------------- 无 VIN 组合分支
def _decide_composite(record, identity, viable, event, policy_note, compared):
    skey = source_key_of(record)
    if identity["confidence"] == "weak":
        return None  # 交给主流程的 insufficient_identity_fields
    for cand, evidence in viable:
        if cand["data"].get("vin"):
            # 候选有 VIN 而当前记录缺失 VIN：证据不对称，禁止自动并入。
            return _result(
                "review", "vin_missing_against_vin_candidate", skey,
                evidence + [_cross_note(record, cand)], policy_note)
        if _count_hard_identity(evidence):
            # 车型/地址对不上：相似但不同车辆，明确保留而非合并。
            return _result(
                "keep", "similar_but_distinct_vehicle", skey,
                evidence + [_cross_note(record, cand)], policy_note)
        hard_matches = [e["field"] for e in evidence
                        if e["field"] in ("model", "address", "mileage")
                        and e["verdict"] == "match"]
        if len(hard_matches) >= 2:
            # 同来源的新版本走编辑路径；跨来源的组合匹配只敢待审，
            # 因为没有 VIN 兜底，自动合并误判代价高。
            if cand["source_key"] == source_key_of(record):
                return _result("merge", "composite_edit", cand["group"],
                               evidence, policy_note)
            return _result(
                "review", "composite_match_needs_review", cand["group"],
                evidence + [_cross_note(record, cand)], policy_note)
    return None


# ------------------------------------------------------------------ 小工具
def _same_source(compared, skey):
    for cand, _evidence in compared:
        if cand["source_key"] == skey:
            return cand
    return None


def _cross_note(record, cand):
    return {
        "type": "store_lineage",
        "current_src": record.get("src"),
        "candidate_src": cand["data"].get("src"),
        "cross_store": record.get("src") != cand["data"].get("src"),
        "candidate_source": cand["source_key"],
        "candidate_group": cand["group"],
    }


def _mileage_conflict_in_event(event):
    diff = event["changes"].get("mileage")
    return bool(diff and diff["old"] is not None and diff["new"] is not None
                and diff["old"] != diff["new"])


def _retransmit_evidence(event, cand):
    return [{
        "type": "retransmit",
        "content_hash": event["content_hash"],
        "target_group": cand["group"] if cand else None,
    }]


def _replay_evidence(event, cand):
    return [{
        "type": "replay",
        "changes": sorted(event["changes"].keys()),
        "target_group": cand["group"] if cand else None,
    }]


def _weak_evidence(identity, event):
    return [{
        "type": "weak_identity",
        "missing_fields": identity["missing"],
        "event": event["kind"],
    }]


def _new_listing_evidence(identity, event, candidates):
    return [{
        "type": "new_listing",
        "block_key": identity["block_key"],
        "candidates_considered": len(candidates),
        "event": event["kind"],
    }]


def _result(outcome, reason, target_group, evidence, policy_note):
    return {
        "outcome": outcome,
        "reason": reason,
        "target_group": target_group,
        "evidence": evidence,
        "policy": policy_note,
    }


# ------------------------------------------------------------------ 阶段入口
def process(record, ctx=None):
    rec = dict(record)
    if ctx is None:  # 无账本的独立调用：退化为保留，保证阶段可单独运行。
        rec[DECISION_OUT] = _result("keep", "no_session",
                                    source_key_of(rec), [], {})
    else:
        rec[DECISION_OUT] = decide(rec, ctx)
    rec.setdefault("_stage_dedup", "decided")
    return rec
