# autotrail — 多车源平台抓挂牌，状态流转、VIN/里程去重、挂牌/成交价历史。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线和幂等性检查
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`vin_id`, `snapshot`, `fetch`, `dedup`, `identity`, `statusline`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 模块说明
业务模块按独立阶段组织，通用模块负责加载、规范化、主键、存储和编排。
流水线按配置顺序调用处理阶段，输入输出均遵循项目定义的记录结构。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。

## 挂牌状态事件机（`statusline.py`）
挂牌状态以“事件历史 → 当前投影”的方式维护，而不是就地覆盖字段。

- **事件**：`(vehicle_key, status, event_time, collected_at)` 经 `keyer.event_key_of`
  派生稳定事件键；`store.events` 只增不改，重复事件按事件键消重。
- **确定性**：当前状态由该车全部事件按 `(event_time, collected_at, 状态优先级, event_key)`
  排序后纯折叠得到。输入顺序、迟到、重复都不影响当前值或转移历史。
- **状态**：`active`（在售）、`reserved`（预订）、`sold`（成交）、`delisted`（下架）、
  `withdrawn`（撤销）、`relisted`（重新挂牌）。
- **迟到/回退**：曾经成交、之后收到**更早**的在售记录 → 当前状态仍为 `sold`（更早的在售
  只是起点）。只有事件时间晚于成交的在售才构成重新挂牌。
  确定答案见 `statusline.resolve_late_active_after_sold(records)`。
- **同一时刻冲突**：先比采集时间（更晚的观测胜出），采集时间相同再按固定优先级
  `sold > withdrawn > delisted > reserved > relisted > active` 裁决。裁决依据
  `adjudicated_by` 与冲突分组 `conflict_group` 随事件历史一起落库。
- **撤销恢复**：`withdrawn` 保存撤销前状态到 `restore_position`，随后的 `relisted`
  恢复到该位置（无历史时回到 `active`），同样随历史持久化。

### 启动持久化
```python
from autotrail import store, pipeline

con = store.connect()
receipt = store.ingest(con, record)      # 单条：返回提交回执/错误分类
pipeline.run_events(records, con=con)    # 批量：逐条回执 + 提交确认 + 错误台账
store.recover(con)                       # 以历史为准修复缺失/过期投影
store.error_log(con)                     # 查询失败事件的重试分类
```

`ingest` 在 SAVEPOINT 内完成“历史写入 + 投影更新”，失败整体回滚并按
`duplicate / invalid / transient / unexpected` 分类记录到 `event_errors`。
若历史已提交而投影未更新（进程中断），`recover()` 会从事件历史重算，使二者一致。

旧接口保持兼容：`statusline.process()` 仍只补 `_stage_statusline` 标记，
`pipeline.run()` 仍是逐记录幂等路径。
