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

## 历史快照与 as-of 查询（snapshot）
`snapshot` 作为流水线最后一个处理阶段，把每条记录同时写入旧 `records` 当前视图与只追加的
`snapshots` 历史表，两边在同一事务内由一次 commit 确认。

双时态语义：
- **有效时间 `valid_time`**：价格/里程/状态在真实世界生效的时间点，取自记录自带 `ts`（统一归一为 UTC `Z`）。
  as-of 查询只按它回答"某一天这辆车当时挂多少钱、什么状态"。
- **采集时间 `captured_at`**：本系统实际抓取写入的处理时钟。事后补录旧挂牌时 `valid_time` 在过去、
  `captured_at` 在现在，两条时间轴互不回改；采集时间不参与去重指纹。

表结构：
- `snapshots`：只追加、永不更新。业务指纹（车键 + 有效时间 + 来源 + 价/里程/状态）为主键，
  同一时刻重复抓取不可重复写入；不同来源、同刻不同报价各自成行。
- `current_state`：每车一行的当前视图，始终取该车辆 `valid_time` 最新的点，可由 `snapshots` 完整重建，
  乱序补录不会让当前视图回退。

主要接口（`autotrail.snapshot`）：
- `append(con, record)`：幂等追加历史点并刷新该车当前视图；
- `as_of(con, vehicle_key, day=...)` / `as_of(..., at=...)`：按天（含当天）/时刻查当时价格与状态，
  同刻多来源报价通过 `rows` 返回，`latest` 为跨来源决胜点；
- `history(con, vehicle_key)`：同一车辆不可重复的时间序列；
- `current_state(con, ...)`：当前视图读取；`rebuild_current(con)` / `recover(con)`：从历史重建/崩溃恢复。

写入一半退出时，未 commit 的事务随连接关闭整体回滚；`recover` 以历史为准重建视图，
随后用原输入重跑流水线即可幂等补齐，已确认的成交证据与旧 `records` 接口输出都不受影响。

