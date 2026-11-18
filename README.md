# autotrail — 多车源平台抓挂牌，状态流转、VIN/里程去重、挂牌/成交价历史。

运行栈：Python 3.11+ 与标准库 `sqlite3`（离线，无需联网；测试用 `pytest`）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量、落库行数与比较量统计
make test    # 运行项目测试，包括流水线、幂等性与去重决策
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段并把会话账本
贯穿各阶段，不在编排层加入业务判断。
业务模块：`vin_id`, `snapshot`, `fetch`, `dedup`, `identity`, `statusline`。
通用模块：`loader / normalizer / keyer / store / journal / pipeline / cli`。

## 去重决策（identity → statusline → dedup）
身份归并的候选不等于可直接覆盖的历史挂牌。三个阶段分工：

- `identity`：输出车辆身份与候选桶。有 VIN 以 VIN 为身份；无 VIN 用
  车型+地址+里程段组合身份（只用于生成候选，不直接等同同一辆车）；
  关键字段缺失时标记 `weak`，下游禁止自动合并。
- `statusline`：对照同来源上一版本输出事件——`new / retransmit / edit /
  relist / replay`，并附逐字段 diff。
- `dedup`：组合 VIN、里程、车型、地址、价格变化形成**可解释候选**，逐字段
  生成 `match / change / conflict / missing` 证据，输出三种裁决：
  - `merge`：重传、编辑、同门店重新挂牌、同车跨门店转载（含门店血缘证据）；
  - `keep`：真正新增、相似但不同车辆、同 VIN 但车型硬冲突；
  - `review`：里程冲突（默认保守）、证据不对称、身份字段缺失。

### 同 VIN 里程冲突的两种策略与代价
- 保守拒绝（默认 `mileage_conflict=review`）：不自动合并，代价是同一辆车
  暂时分裂为多条挂牌，需人工复核归并；
- 积极合并（`mileage_conflict=merge`）：接受里程差异，代价是 VIN 误录的
  不同车辆会被错误并入，原始里程证据仍保留可追溯。

代价说明随决策证据一并落库。

## 持久化、可撤销与可重放
SQLite 内三张表（见 `store.py`）：
- `records`：稳定键 upsert 的当前版本视图（旧键规则保持兼容）；
- `listing_sources`：来源挂牌**只追加**版本日志，历史回放标记
  `is_current=0`，永不覆盖，撤销后来源可完整恢复；
- `decisions`：决策只追加账本，每条带 `evidence / policy / source_seq`，
  撤销即追加 `undo_merge` 反向决策并标记旧决策，分组沿合并边重算恢复。

决策 ID 由来源键、版本号与内容指纹确定性派生：同输入重放得到同一 ID，
整轮重跑不新增版本与决策（结果稳定、幂等、可重放）。

## 比较量
dedup 只在身份桶内比较，不做全量历史扫描；`Session.comparisons` 累计字段
级比较次数，`block_sizes` 记录各候选桶规模，比较量随桶规模线性增长并随
运行结果输出，便于评估候选规模扩大时的成本。

## 测试
`tests/test_pipeline.py` 覆盖旧夹具兼容与幂等；`tests/test_dedup.py` 把
重传、编辑、撤销合并与真正新增交错输入，覆盖冲突字段、同车跨门店、同门店
重新挂牌、字段缺失、相似但不同车辆、历史回放，并验证撤销后来源可恢复、
决策确定性与比较量增长。
