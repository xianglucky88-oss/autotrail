"""单一职责：去重决策的运行时会话与只追加账本。

Session 把 sqlite 账本封装成阶段可用的视图，自身不含业务规则：
- 来源版本：每个 source_key 下只追加版本，seq 单调递增，旧版本永不覆盖；
- 候选索引：按 identity 的 block_key 组织已收录来源的最新版本，
  dedup 只在同桶内比较，比较量随桶规模而非全量历史增长；
- 分组解析：合并决策形成 source -> group 的有向边，分组沿边解析；
- 撤销：追加反向决策并标记旧决策，重算边即可恢复来源独立身份；
- 比较量：comparisons 记录字段级比较次数，block_sizes 记录各桶规模。
"""
from . import store
from .identity import identity_of
from .keyer import source_key_of, decision_id

# 默认策略：同 VIN 里程冲突保守待审。可在构造 Session 时覆盖。
DEFAULT_POLICY = {"mileage_conflict": "review"}


class Session:
    def __init__(self, con, policy=None):
        self.con = con
        self.policy = dict(DEFAULT_POLICY, **(policy or {}))
        # source_key -> {"seq": int, "data": dict}（每个来源的最新版本）
        self._latest = {}
        # source_key -> group 边（仅合并决策产生）
        self._edges = {}
        # source_key -> 最新一条有效决策
        self._latest_decision = {}
        # block_key -> set[source_key]
        self._blocks = {}
        # 已 pre_index 但尚未 ingest 落账的来源（候选查询时排除自身）
        self._inflight = set()
        self.comparisons = 0
        self.block_sizes = {}
        self._hydrate()

    def reset_counters(self):
        """重置本轮统计（账本本身不变），使比较量只反映本次运行。"""
        self.comparisons = 0
        self.block_sizes = {}

    # ------------------------------------------------------------------ 装载
    def _hydrate(self):
        # 只把当前版本装入最新视图；历史回放版本留在账本可追溯，不参与候选。
        for source_key, seq, data in store.iter_current_sources(self.con):
            self._latest[source_key] = {"seq": seq, "data": data}
            for block in identity_of(data)["block_keys"]:
                self._blocks.setdefault(block, set()).add(source_key)
        for dec in store.all_decisions(self.con):
            self._apply_decision(dec)

    def _apply_decision(self, dec):
        skey = dec["source_key"]
        if dec["outcome"] == "undo_merge":
            # 反向决策：被撤销的合并边随 undone 标记失效，重水合时直接剔除。
            self._edges.pop(skey, None)
            self._latest_decision[skey] = dec
            return
        self._latest_decision[skey] = dec
        if dec["outcome"] == "merge" and not dec.get("undone_by"):
            self._edges[skey] = dec["target_group"]
        elif dec["outcome"] == "merge" and dec.get("undone_by"):
            self._edges.pop(skey, None)

    # ------------------------------------------------------------- 来源版本
    def latest_seq(self, source_key):
        """以账本当前版本（排除历史回放）为准的最新 seq。"""
        return store.latest_source_seq(self.con, source_key)

    def latest_source(self, source_key):
        item = self._latest.get(source_key)
        return item["data"] if item else None

    def persisted_previous(self, source_key):
        """已落账的上一版本（忽略 pre_index 占位），供 statusline 定事件。"""
        if source_key in self._inflight:
            seq = store.latest_source_seq(self.con, source_key)
            if seq == 0:
                return None, None
            return store.get_source(self.con, source_key, seq), seq
        item = self._latest.get(source_key)
        if not item:
            return None, None
        return item["data"], item["seq"]

    def existing_version(self, source_key, digest):
        """若该来源任一已落账版本内容指纹相同，返回 (seq, 对应决策)，否则 None。

        用于重传/整轮回放的幂等：相同内容不重复追加版本与决策，旧决策 ID
        与旧分组保持稳定（旧结果兼容、结果可重放）。检查覆盖历史回放版本
        （is_current=0），并按版本顺序返回该版本自己的决策，而不是来源的
        最新决策——否则回放记录在二次喂入时会被错误替换成最新版本的决策。
        """
        from .keyer import content_hash
        matched_seq = None
        for _skey, seq, data in store.iter_sources(self.con):
            if _skey == source_key and content_hash(data) == digest:
                matched_seq = seq
                break
        if matched_seq is None:
            return None
        # 决策直接携带所属来源版本号，回放版本（seq 与决策顺序不一致时）
        # 也能精确取回自己的决策。
        for dec in store.decisions_for(self.con, source_key):
            if dec["outcome"] != "undo_merge" \
                    and dec.get("source_seq") == matched_seq:
                return matched_seq, dec
        return matched_seq, self._latest_decision.get(source_key)

    def restore_latest(self, source_key):
        """回放后把内存最新版本视图恢复为账本中的最新版本。"""
        seq = store.latest_source_seq(self.con, source_key)
        if seq:
            self._latest[source_key] = {
                "seq": seq,
                "data": store.get_source(self.con, source_key, seq),
            }

    def pre_index(self, source_key, data):
        """决策前把当前版本纳入内存候选视图。

        账本尚未追加（决策还没产生）：先以 seq=0 占位，使 statusline 能
        读到同来源上一版本、dedup 能在同桶内看到候选；候选查询用
        exclude_source 排除自身。ingest 时再以正式 seq 落账覆盖占位。
        """
        prev = self._latest.get(source_key)
        seq = (prev["seq"] + 1) if prev else 0
        self._latest[source_key] = {"seq": seq, "data": data}
        for block in identity_of(data)["block_keys"]:
            bucket = self._blocks.setdefault(block, set())
            bucket.add(source_key)
        self._inflight.add(source_key)
        return block

    def ingest(self, source_key, seq, ingest_ts, data, decision,
              historical=False):
        """落账：来源版本与决策同事务追加，全部幂等。

        historical=True 时为历史回放：版本仍追加留痕（可追溯、可重放），
        但标记 is_current=0，不更新最新版本视图与候选索引，后续事件判定
        仍以最新版本为准。
        """
        store.append_source(self.con, source_key, seq, ingest_ts, data,
                            is_current=not historical)
        store.append_decision(self.con, decision)
        self._inflight.discard(source_key)
        if historical:
            return
        # 更新内存视图，供同一次运行的后续记录使用。
        self._latest[source_key] = {"seq": seq, "data": data}
        for block in identity_of(data)["block_keys"]:
            bucket = self._blocks.setdefault(block, set())
            bucket.add(source_key)
            self.block_sizes[block] = len(bucket)
        self._latest_decision[source_key] = decision
        if decision["outcome"] == "merge":
            self._edges[source_key] = decision["target_group"]

    # ---------------------------------------------------------------- 候选
    def candidates(self, block_key, exclude_source):
        """同候选桶内除自身外的已收录来源最新版本。"""
        members = self._blocks.get(block_key, set())
        out = []
        for skey in sorted(members):
            if skey == exclude_source and skey in self._inflight:
                # 同来源的决策前占位：候选视图返回账本里的上一版本，
                # 供重传/编辑/重挂/回放路径做血缘与字段对照；
                # 首次出现（无上一版本）时仅跳过候选，桶成员仍然保留。
                prev, prev_seq = self.persisted_previous(skey)
                if prev is None:
                    continue
                latest_dec = self._latest_decision.get(skey)
                out.append({
                    "source_key": skey,
                    "seq": prev_seq,
                    "data": prev,
                    "group": self.group_of(skey),
                    "pending": bool(latest_dec and
                                    latest_dec["outcome"] == "review"),
                    "decision_reason": latest_dec["reason"]
                    if latest_dec else None,
                    "inflight": True,
                })
                continue
            if skey in self._inflight:
                continue  # 其他来源尚未落账，不参与本次裁决
            item = self._latest[skey]
            latest_dec = self._latest_decision.get(skey)
            out.append({
                "source_key": skey,
                "seq": item["seq"],
                "data": item["data"],
                "group": self.group_of(skey),
                "pending": bool(latest_dec and
                                latest_dec["outcome"] == "review"),
                "decision_reason": latest_dec["reason"] if latest_dec else None,
            })
        return out

    def note_block_size(self, block_key):
        # 无论桶内是否有候选都记录规模，新桶（无候选）规模为 0（不含自身）。
        size = len(self._blocks.get(block_key, set()))
        self.block_sizes[block_key] = size
        return size

    def count_comparison(self, n=1):
        self.comparisons += n

    # ---------------------------------------------------------------- 分组
    def group_of(self, source_key):
        """沿合并边解析到分组根；无合并边时来源自身即分组根。"""
        seen = set()
        cur = source_key
        while cur in self._edges and cur not in seen:
            seen.add(cur)
            cur = self._edges[cur]
        return cur

    def group_members(self, group):
        return sorted(s for s in self._latest if self.group_of(s) == group)

    # ---------------------------------------------------------------- 决策
    def make_decision_id(self, source_key, seq, digest):
        return decision_id(source_key, seq, digest)

    def decision_for(self, decision_id):
        return store.get_decision(self.con, decision_id)

    def decisions_for_source(self, source_key):
        return store.decisions_for(self.con, source_key)

    def undo_merge(self, merge_decision, undo_ts):
        """撤销一条合并决策：追加反向决策并标记旧决策，来源分组自动恢复。"""
        skey = merge_decision["source_key"]
        undo_id = self.make_decision_id(
            skey, self.latest_seq(skey),
            "undo:" + merge_decision["decision_id"],
        )
        if store.get_decision(self.con, undo_id):
            return store.get_decision(self.con, undo_id)
        undo = {
            "decision_id": undo_id,
            "created_ts": undo_ts,
            "source_key": skey,
            "source_seq": self.latest_seq(skey),
            "outcome": "undo_merge",
            "reason": "manual_undo",
            "target_group": skey,
            "evidence": [{
                "type": "undo",
                "reinstates_source": skey,
                "supersedes": merge_decision["decision_id"],
            }],
            "policy": self.policy,
            "undone_by": None,
            "supersedes": merge_decision["decision_id"],
        }
        store.append_decision(self.con, undo)
        store.mark_undone(self.con, merge_decision["decision_id"], undo_id)
        self._edges.pop(skey, None)
        self._latest_decision[skey] = undo
        return undo
