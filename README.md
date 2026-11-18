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

## VIN/里程身份解析（vin_id）
流水线顺序：`loader → normalizer → vin_id → fetch → identity → statusline → dedup → keyer → store`。
`vin_id.process()` 是无状态的单记录确定性变换，把**原始标识、校验结果、标准身份、里程观测**分开存放：

- 原始 VIN 留在 `vin`，规范化结果（去分隔符、转大写、遮挡位统一 `*`）写入 `vin_identity.normalized_vin`；
- 完整 17 位 VIN 按标准校验位算法（SAE J853 / GB 16735）校验，结果在 `vin_identity.check`；
- 里程只进 `mileage_obs`（含来源与时间戳），**永不参与主键**——同 VIN 更新里程仍是同一辆车；
- 多来源与字段冲突留痕在 `sources` / `field_claims`，原始字段不覆盖。

四类键（同输入恒同键，跨来源重复运行得到同一主键）：

| 键前缀 | 触发条件 | 能否作为车辆主键 |
| --- | --- | --- |
| `vin:` | 17 位完整 VIN 且校验通过 | 可以，唯一强键 |
| `vinpart:` | 遮挡或截断的部分 VIN | 弱键：仅同骨架聚簇，禁止把不同完整 VIN 自动并车 |
| `vinbad:` | 17 位但非法字符/校验位失败，或长度异常 | 不可以：仅聚簇同一条坏号的多次提交，等待补号 |
| `fallback:` | 缺 VIN（部分/坏 VIN 也同时携带） | 降级键：仅由车牌等稳定挂牌属性派生（不含里程），跨来源不可直接视为同车 |

`completion_hint.fallback_key` 在补号前后保持不变：坏号/遮挡记录补齐为完整 VIN 后，
可凭它把新的 `vin:` 强键认出是原记录的延续。`keyer` 只读 `vin_identity.key`，
不读里程；老格式记录（无 VIN/车牌/里程等车辆字段）原样透传，仍走 `id`/哈希旧键路径。
`store.upsert` 默认覆盖写保持基线行为，流水线按 `vin_id.MERGE_LIST_FIELDS` 对
里程观测、来源、字段留痕做去重合并。

### 离线身份数据集
三组离线数据随仓库放在 `tests/fixtures_offline/<数据集>/input.json`，由
`tests/test_offline_identity.py` 加载（也可用环境变量 `AUTOTRAIL_OFFLINE_DATA`
指向外部目录换数据复跑），每个用例使用独立临时 sqlite：

1. **遮挡号码碰撞**：同骨架的两种遮挡写法合并为一个弱键；构造出的另一辆完整 VIN 在同一
   遮挡模板下骨架相同，却保留独立 `vin:` 键（弱键碰撞不得误并车）；
2. **校验失败后补齐号码**：截断 → 坏校验位聚簇 → 合法完整 VIN，三类键各自独立，
   靠恒定 `fallback_key` 桥接，坏号原文与错误原因全程留痕；
3. **提交中断**：`commit` 失败时事务整体回滚不留半截状态，重启后主键与中断前内存计算
   逐一吻合；里程更新与重复提交不新增车辆行。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
