"""三组离线数据的端到端身份测试：遮挡碰撞、校验失败补号、提交中断/重启。

数据集随仓库放在 ``tests/fixtures_offline/``（每个数据集一个独立目录）；
若设置了 ``AUTOTRAIL_OFFLINE_DATA`` 环境变量，则改用该外部目录，便于换数据复跑。
每个用例使用独立临时 sqlite，不触碰仓库默认库。
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autotrail import pipeline, store, vin_id, keyer  # noqa: E402

DATA_DIR = Path(os.environ.get("AUTOTRAIL_OFFLINE_DATA",
                               ROOT / "tests" / "fixtures_offline"))


# ---- 离线流水线工具：把外部数据集当作 loader 源，跑完整链条 ------------------------

def run_offline(dataset, db_path, monkeypatch):
    records = json.loads((DATA_DIR / dataset / "input.json").read_text(encoding="utf-8"))
    monkeypatch.setattr("autotrail.config.DB_PATH", db_path)
    monkeypatch.setattr("autotrail.store.DB_PATH", db_path)
    monkeypatch.setattr("autotrail.loader.FIXTURES", DATA_DIR / dataset / "input.json")
    return records, pipeline.run()


def rows_of(db_path):
    con = sqlite3.connect(db_path)
    try:
        return store.all_records(con)
    finally:
        con.close()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "offline.db")


# ---- 数据集 1：遮挡号码碰撞 ------------------------------------------------------

def test_masked_collision_merge_and_no_false_merge(db_path, monkeypatch):
    records, stats = run_offline("masked_collision", db_path, monkeypatch)
    assert stats["fed"] == 6

    processed = [vin_id.process(r) for r in records]
    masked_key = keyer.key_of(processed[0])
    # 同一遮挡骨架、两种分隔/大小写写法 → 同一弱键
    assert keyer.key_of(processed[1]) == masked_key
    full_a = keyer.key_of(processed[2])
    full_e = keyer.key_of(processed[3])
    # 完整 VIN 各自成键，绝不与遮挡弱键、也不互相并车
    assert full_a.startswith("vin:") and full_e.startswith("vin:")
    assert len({masked_key, full_a, full_e}) == 3
    # 真实碰撞证据：VIN_E 在同一遮挡模板下的骨架与遮挡记录一致
    holes = (8, 10, 11, 13)
    proj_e = "".join("*" if i in holes else c for i, c in enumerate(records[3]["vin"]))
    assert vin_id._masked_skeleton(proj_e) == vin_id._masked_skeleton(
        vin_id.normalize_vin(records[0]["vin"])[0])

    rows = dict(rows_of(db_path))
    # 6 条 → 5 行：两条遮挡记录合并，其余各自身份独立
    assert stats["rows"] == 5
    merged = rows[masked_key]
    sources = {s["src"] for s in merged["sources"]}
    assert sources == {"alpha", "beta"}                      # 两个来源都留痕
    assert [o["mileage"] for o in merged["mileage_obs"]] == [52000, 53500]
    colors = {c["value"] for c in merged["field_claims"] if c["field"] == "color"}
    assert colors == {"white", "pearl-white"}               # 字段冲突可审计
    # 同车牌的三条身份共享同一 fallback 桥接键
    fb = {processed[i]["vin_identity"]["fallback"]["key"] for i in range(3)}
    assert len(fb) == 1


# ---- 数据集 2：校验失败后补齐号码 -------------------------------------------------

def test_invalid_vin_then_completion_keeps_audit_trail(db_path, monkeypatch):
    records, stats = run_offline("invalid_then_completed", db_path, monkeypatch)
    assert stats["fed"] == 5
    processed = [vin_id.process(r) for r in records]
    keys = [keyer.key_of(p) for p in processed]

    truncated, bad1, bad2, completed, no_vin = processed
    # 截断 → 弱键；坏校验位两次提交 → 同一 vinbad 聚簇键
    assert truncated["vin_identity"]["kind"] == "partial_truncated"
    assert bad1["vin_identity"]["kind"] == bad2["vin_identity"]["kind"] == "invalid"
    assert keys[1] == keys[2] and keys[1].startswith("vinbad:")
    # 坏 VIN 原文与错误原因保留
    assert bad1["vin_identity"]["raw_vin"] == records[1]["vin"]
    assert bad1["vin_identity"]["check_error"] == "check-digit-mismatch"
    # 补齐后 → 完整强键；携带 completion_hint 指向恒定 fallback
    assert completed["vin_identity"]["kind"] == "full"
    assert keys[3].startswith("vin:")
    assert completed["vin_identity"]["completion_hint"]["fallback_key"] == \
        bad1["vin_identity"]["fallback"]["key"]
    # 五条记录同车牌 → fallback 全部一致，补号前后可凭它认出原记录
    fb = {p["vin_identity"]["fallback"]["key"] for p in processed}
    assert len(fb) == 1
    # 无 VIN 记录走纯降级键
    assert no_vin["vin_identity"]["kind"] == "fallback"
    assert keys[4] == next(iter(fb))

    rows = dict(rows_of(db_path))
    # 坏 VIN 两次提交合并为一行，里程观测累积、原始来源都在
    bad_row = rows[keys[1]]
    assert [o["mileage"] for o in bad_row["mileage_obs"]] == [31000, 31500]
    assert {s["src"] for s in bad_row["sources"]} == {"alpha", "beta"}
    assert stats["rows"] == 4  # truncated / bad-cluster / completed / fallback


# ---- 数据集 3：提交中断 + 重启前后主键一致 ----------------------------------------

def test_commit_interrupt_leaves_no_partial_state(db_path, monkeypatch):
    records = json.loads((DATA_DIR / "commit_interrupt" / "input.json").read_text())
    monkeypatch.setattr("autotrail.config.DB_PATH", db_path)
    monkeypatch.setattr("autotrail.store.DB_PATH", db_path)
    monkeypatch.setattr("autotrail.loader.FIXTURES",
                        DATA_DIR / "commit_interrupt" / "input.json")

    # 中断：commit 抛错，连接随 finally 关闭，未提交事务整体回滚
    import autotrail.store as store_mod
    real_commit = store_mod.commit

    def boom(_con):
        raise sqlite3.OperationalError("simulated commit interruption")

    monkeypatch.setattr(store_mod, "commit", boom)
    with pytest.raises(sqlite3.OperationalError):
        pipeline.run()
    assert not Path(db_path).exists() or rows_of(db_path) == []

    # “重启”：恢复 commit 后重跑，键必须与中断前在内存中算出的完全一致
    monkeypatch.setattr(store_mod, "commit", real_commit)
    stats = pipeline.run()
    assert stats["fed"] == 5

    pre_restart_keys = sorted({keyer.key_of(vin_id.process(r)) for r in records})
    post_rows = sorted(rows_of(db_path))
    post_restart_keys = sorted(k for k, _ in post_rows)
    assert pre_restart_keys == post_restart_keys

    # 同 VIN 多次（含完全重复）+ 一次遮挡 → 2 行；里程不制造新车
    assert stats["rows"] == 2
    full_key = next(k for k in post_restart_keys if k.startswith("vin:"))
    full_row = dict(post_rows)[full_key]
    assert [o["mileage"] for o in full_row["mileage_obs"]] == [12000, 15600, 15900]
    assert {s["src"] for s in full_row["sources"]} == {"alpha", "beta"}
    # 完全重复的第五条被幂等去重：12000 只保留一次
    assert sum(o["mileage"] == 12000 for o in full_row["mileage_obs"]) == 1


def test_repeated_runs_are_idempotent_same_keys(db_path, monkeypatch):
    records, first = run_offline("commit_interrupt", db_path, monkeypatch)
    second = pipeline.run()  # 跨来源重复运行
    assert second["rows"] == first["rows"]
    keys_run1 = sorted(k for k, _ in rows_of(db_path))
    pipeline.run()
    keys_run2 = sorted(k for k, _ in rows_of(db_path))
    assert keys_run1 == keys_run2
    # 与直接逐记录派生的键逐一吻合（确定性，不依赖运行次数）
    assert keys_run2 == sorted({keyer.key_of(vin_id.process(r)) for r in records})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
