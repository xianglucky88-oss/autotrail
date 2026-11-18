"""薄编排：loader → normalizer→fetch→identity→statusline→dedup → keyer → store。

不含业务判断：业务规则全部位于 identity / statusline / dedup；
pipeline 只负责把 Session 账本贯穿各阶段，并把带依据的决策随 keyer 落库。
"""
import importlib

from . import store as _store
from . import loader as _loader
from .journal import Session
from .keyer import key_of, source_key_of, content_hash
from .models import Record

CHAIN_NAMES = ['normalizer', 'fetch', 'identity', 'statusline', 'dedup']


def ingest_record(raw, ctx, con):
    """对单条原始记录跑完整阶段链并落账（交错输入入口）。

    与 run() 共用同一条链与同一套幂等规则，测试可按任意顺序喂入
    重传/编辑/撤销/新增，验证结果稳定且可撤销。
    """
    chain = [
        importlib.import_module("." + n, __package__).process
        for n in CHAIN_NAMES
    ]
    # 指纹与来源键必须基于规范化后的业务字段，否则与账本中已落账版本
    # （同样经过 normalizer）对不上，重放会被误判为新版本。
    normalized = chain[0](dict(raw))
    skey = source_key_of(normalized)
    digest = content_hash(normalized)
    existing = ctx.existing_version(skey, digest)

    if existing is not None:
        seq, decision = existing
        decision_id = decision["decision_id"]
        rec = normalized
        for fn in chain[1:]:
            rec = fn(rec, ctx)
    else:
        # 决策前进入候选视图（仅内存），dedup 才能看到同来源上一版本与同桶候选。
        ctx.pre_index(skey, normalized)
        rec = normalized
        for fn in chain[1:]:
            rec = fn(rec, ctx)
        # 正式版本号以账本为准（pre_index 的占位不占 seq）。
        seq = _store.latest_source_seq(con, skey) + 1
        historical = rec["_event"]["kind"] == "replay"
        decision = dict(rec["_dedup"])
        decision_id = ctx.make_decision_id(skey, seq, digest)
        decision.update({
            "decision_id": decision_id,
            "created_ts": rec.get("ts") or f"seq:{seq}",
            "source_key": skey,
            "source_seq": seq,
            "undone_by": None,
            "supersedes": None,
        })
        ctx.ingest(skey, seq, decision["created_ts"], rec, decision,
                   historical=historical)
        if historical:
            # 回放不改变最新版本视图：恢复账本中的最新版本，候选索引保持原状。
            ctx.restore_latest(skey)

    rec = dict(rec)
    rec["_decision_id"] = decision_id
    rec["_source_key"] = skey
    rec["_source_seq"] = seq
    rec["_group"] = decision.get("target_group")
    rec["_decision"] = decision
    if decision.get("reason") != "historical_replay":
        # records 表是“当前版本”视图：历史回放只进只追加账本留痕，
        # 不覆盖当前挂牌，保证旧结果与最新状态不被回放改写。
        _store.upsert(con, Record(key=key_of(rec), data=rec))
    return rec


def run(con=None, policy=None):
    own = con is None
    con = con or _store.connect()
    ctx = Session(con, policy=policy)
    ctx.reset_counters()
    try:
        chain = [
            importlib.import_module("." + n, __package__).process
            for n in CHAIN_NAMES
        ]
        fed = 0
        for raw in _loader.load():
            ingest_record(raw, ctx, con)
            fed += 1

        _store.commit(con)
        return {
            "fed": fed,
            "rows": _store.count(con),
            "comparisons": ctx.comparisons,
            "block_sizes": dict(sorted(ctx.block_sizes.items())),
        }
    finally:
        if own:
            con.close()
