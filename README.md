# autotrail — 多车源平台抓挂牌，状态流转、VIN/里程去重、挂牌/成交价历史。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线、幂等性与身份归并回放
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`vin_id`, `snapshot`, `fetch`, `dedup`, `identity`, `statusline`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 无完整 VIN 的挂牌身份归并
车源里没有完整 VIN 的记录既不丢弃，也不能仅凭车型相同并为一辆车。归并分两层：

### 阶段职责
- `vin_id`：只管号码本身——规范化、`full / partial / absent / invalid` 分类、
  完整 17 位 VIN 的 ISO 3779 校验位核验，结果挂在记录的 `_vin` 字段。
- `identity`：在 `_vin` 之上维护 `VehicleIdentity` 稳定对象（候选身份、别名、
  成员挂牌、逐因素证据与置信度），输出 `_identity` 块。完整号码解析仍由
  `vin_id` 负责；`process(record)` 调用方式与 `keyer` 按记录 `id` 读取历史键
  的方式保持不变，状态历史可凭 `_identity.identity_id` 引用稳定对象。

### 证据与三档结论
| 证据因素 | 权重 | 说明 |
| --- | --- | --- |
| 完整 VIN 一致 | 100 | 同号必并；异号、品牌/车型/年份不一致为硬否决（veto） |
| 车牌脱敏片段一致 | 45 | 至少 4 位字母数字才采信 |
| 品牌+车型 | 12 | 仅此项不足以并车 |
| 年份 / 配置 | 8 / 7 | 年份不一致硬否决；配置冲突只降级不否决 |
| 里程 / 价格 | 6 / 4 | 接近为弱证据（3000km、5% 容差） |
| 不完整 VIN 尾号 / 同车源 | 8 / 2 | 弱证据；不同门店不扣分，允许同车换店 |

- `confirmed`：无字段冲突且加权分 ≥ 70，或完整 VIN 同号——自动确认；
- `pending`：分数 ≥ 35、共享强别名但证据不足，或存在字段冲突——只能待审，
  生成无向 link 待审单，交人工裁决（`IdentityRegistry.resolve_review`）；
- `new`：硬否决或强别名（车牌+VIN 尾号）双双对不上——另立身份，不制造待审噪音。

### 回放冻结与顺序无关
- **冻结保护**：身份一旦 `confirmed`，流式模式下新线索再强也只生成 open
  待审，确认归属（成员集合）不会被无声改写；同 VIN 但字段硬冲突同样只待审。
- **顺序无关批处理**：`identity.resolve_batch(records)` 用两两对称评分 +
  并查集连通分量 + 确定性锚点（成员最小挂牌键派生 `identity_id`），来源到达
  顺序颠倒后，身份分组、每条记录结论与待审集合完全一致。
- 待审裁决写入只增审计日志；批准后重放归并，驳回则为挂牌另立独立身份。

## 对照数据
`fixtures/identity_cases.json` 按 `_scenario` 分组，覆盖：同车换店（证据齐全 /
缺字段）、两辆配置相同的车（有/无车牌）、字段冲突、转让后重挂牌、完整 VIN
跨店通道与同号冲突、确认后新线索冻结保护。`tests/test_identity.py` 对每个
场景比较缺字段前后与来源顺序颠倒后的归属。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
