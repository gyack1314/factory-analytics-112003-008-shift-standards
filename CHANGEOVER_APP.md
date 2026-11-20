# 换型工时标准与班次承诺重算应用

面向多品种小线排产的换型（changeover）工时管理应用。它不再把换型工时
当成产品族上的一个常数，而是基于工单切换、清洗签字、首件检验、量产开始、
工具借用和班次凭证，重建每条换型的时间线，并把四类过去被混在一起的时间
显式分开：

| 口径 | 含义 | 对排产的意义 |
| --- | --- | --- |
| 可重叠时间 `overlap_prep` | 预准备落在停线之前、与上一批运行并行的部分 | 前移到前一班的**人工时间**，不减少产线占用 |
| 技师工时重复计入 `double_counted` | 同一技师的并行工序被逐工序相加产生的虚增 | 并集口径下消失，不能当作换型变长 |
| 换型尚未结束 `censored` | 无量产重启证据或仍有开放工序（如重检进行中） | 不进点估计，只报告已观察时长与截尾数量 |
| 设备故障等待 `equipment_wait` | 无技师的排故等待，可与工序并行 | 从人工口径剥离，单列其压在占线窗口内的部分 |

## 目录结构

```
changeover/
  models.py        输入记录的数据类、装载与跨表校验
  timeutils.py     半开区间运算、班次切分、经验分布与分位数
  parsing.py       单条换型时间线重建（重叠/重复/截尾/等待/工具去重）
  transfer.py      历史迁移判定：材料、洁净等级、模具方向三门槛 + 跨厂关系
  estimator.py     有向切换的工时分布、样本充分性、不确定性与迁移降级
  standards.py     标准台账：版本生效、批准、退回补采、暂时拒用、改版追溯
  resources.py     产线/共用工具/技师资源日历、争抢与首件失败 hold
  scheduling.py    方案构建：占线 vs 前移人工拆分、顺序枚举、增配工具对比
  live.py          执行中换型：锁定开工版本、按实际阶段推算剩余时间
  reconciliation.py 历史班次复核：计划偏差按工序归因
  versions.py      计划版本：锁定交期重排、连锁冲突、原版本与责任班次保留
  cli.py / app.py  命令行入口与装配根
run_cli.py         隔离模式启动器（python3 -I run_cli.py ...）
run_tests.py       测试运行器（python3 -I run_tests.py [-v]）
tests/             71 个单元/端到端用例
output/            各命令的示例输出留痕
state/             标准台账与计划版本的持久化状态（运行时生成）
```

## 运行方式

环境为 Python 3.11，仅使用标准库。推荐用 `-I` 隔离模式运行：

```bash
python3 -I run_tests.py            # 全部测试
python3 -I run_cli.py --help       # 命令清单
python3 -m unittest discover -s tests -v   # 原有资料测试仍可运行
```

## 角色与命令

### 计划员：查标准、比方案、看资源

```bash
# 某有向切换的完整分布（p10/p25/中位/p75/p90、截尾、来源、迁移判定）
python3 -I run_cli.py estimate --pair A,B --factory 一厂 --spec V2
# 全部 36 个 方向×厂×版本 的概览与不确定性带
python3 -I run_cli.py estimates
# 样本不足与完全未覆盖方向（含阻断属性）
python3 -I run_cli.py gaps
# 历史能否迁移的三门槛判定
python3 -I run_cli.py transfer --target-pair A,C --target-site 一厂,L0 \
    --source-pair A,B --source-site 一厂,L1
# 复核时点的模具/技师争抢与受影响产线
python3 -I run_cli.py calendar --review-min 21645
# 基线方案 / 四方案对比（基线、禁提前、最优顺序、增配共用工具）
python3 -I run_cli.py plan --review-min 21645
python3 -I run_cli.py compare --review-min 21645
```

每条标准估计都带三样计划员需要的信息：

* **不确定性**：经验分位带，迁移样本按关系系数（跨厂 1.15、跨族 1.2/1.3）放大；
* **适用版本**：与台账生效时点联动，只返回当时有效口径；
* **样本充分性**：直接完整样本 ≥5 才判充分，否则注明截尾数、迁移来源与缺口。

方案对比输出同时给出 `total_line_minutes`（真正的占线时间）与
`total_overlap_prep_min`（只是前移到前一班的人工），并按班次拆出
`shift_view`，明确哪些人工被移到了前一班。

### 工艺负责人：批准、补采、拒用与追溯

```bash
python3 -I run_cli.py approve --pair A,C --factory 一厂 --spec V2 \
    --effective-min 22000 --by 工艺张 --note 复核批准
python3 -I run_cli.py request-gap --pair B,C --factory 一厂 --spec V2 \
    --reason 洁净等级与模具方向变化阻断迁移 --at-min 21700 --by 工艺张
python3 -I run_cli.py reject --pair A,B --factory 一厂 --spec V2 \
    --condition 重检样本不足3条暂时拒用 --at-min 21700 --by 工艺张
python3 -I run_cli.py revisions --pair A,B --factory 一厂
```

证据不足时批准会自动加注「仅限观察使用」；拒用条件命中后该版本不能用于
排产，条件解除后可恢复。`revisions` 返回改版链、触发它的补采缺口与
当前生效的拒用条件。

### 执行中换型与重排

```bash
# LIVE 按 V1 开工而 V2 已生效：继续用 V1 + 实际阶段，剩余按工序分解
python3 -I run_cli.py live --switch LIVE --review-min 21645
# 未开工订单重排评估（带 --confirm 且可行时才落新版本，原版本保留）
python3 -I run_cli.py replan --review-min 21670 --by 计划员李
# 历史班次复核，偏差追到工序与归因
python3 -I run_cli.py reconcile --from-min 100 --to-min 21600
```

## 关键业务规则的落地位置

1. **方向是有向的**：A→B 与 B→A 分别建分布（`estimator.py` 的 pair 键）。
2. **迁移三门槛**：跨厂家族借用仅在材料、洁净等级、模具方向逐一相同时
   可用，任一变化都给出阻断原因（`transfer.py`）。
3. **首件失败不释放产线**：重检放行前置 `hold_until`，排产器不会把该线
   分给下张订单（`live.py` + `resources.py`）。
4. **新版本不回溯**：执行中换型锁定开工版本，已完成工序以实际凭证为准，
   只对未完成部分按旧版本估计（`live.py`）。
5. **锁定交期重排**：已开工订单禁止重排；未开工订单的新方案必须报告对
   其他锁定订单的连锁冲突，确认后另存版本并记录责任班次（`versions.py`）。
6. **工具日志清洗**：23 条精确重复按区间并集去重；LIVE 与 PLAN1 对 M0 的
   跨线矛盾保留为争抢证据（`parsing.py` + `resources.py`）。

## 数据口径备忘

* 所有时间为相对分钟，区间 `[start, end)` 右端不包含，`null` 表示事实缺失；
* 截尾样本（C0/C13/…/LIVE 共 15 条）不参与点估计；
* `signed_min` 是工时结束证据入档点，稳定晚于重启 5 分钟，不与占线混用；
* 持久化动作只写 `state/`，永不回写 `data/` 原始凭证。
