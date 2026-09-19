# 领域说明

可转债判断依赖债券条款、正股价格、转股价和市场交易日。强赎、回售、下修与转股价调整可能使用不同观察窗口，也可能互相影响后续计算。行情补发、公司行动和公告撤回都需要形成新的可追溯数据版本，历史研究结论应保留引用依据。

`reference/domain.json` 保存可公开的示例枚举与精度约定。正式业务记录应使用稳定标识，并区分业务发生时间、系统接收时间和记录版本。

## 核心概念

### 条款（Clause）

每只债券挂一组条款，类型见 `clause_types`：

- `redemption`（强赎）、`put`（回售）、`conversion_price_reset`（下修触发条件）都是**窗口条款**：
  在 `applies_from` ~ `applies_until` 内，观察最近 `window_days` 个**适用交易日**，
  其中至少 `required_hits` 天满足 `收盘价 {comparison} 当日转股价 × threshold_ratio`。
- `maturity`（到期）报告距离到期的自然日与交易日数。
- 条款可声明 `modifies`（如 `conversion_price`）与 `depends_on` / `window_reset_on`
  （引用其他 clause_id 或条款类型）。引擎据此做拓扑排序：修改转股价或观察期的条款
  先算，读取它们的条款后算；出现循环依赖（含自环、悬空引用）时拒绝求值并给出环路径。
- `window_reset_on: ["conversion_price_reset"]` 表示任何下修生效后观察期重新起算。

### 适用交易日

滚动窗口只计入适用交易日：交易日历内的交易日，且正股未停牌、存在有效官方收盘价。
其余日子逐日分类留痕（`day_statuses`）：

- `suspended`：停牌（已知市场状态，列入证据但不算数据缺口）；
- `missing_price`：无行情记录（数据缺口）；
- `temporary_only`：仅有盘中临时价（数据缺口；临时价绝不参与正式判断）；
- `retracted_price`：行情被供应商撤回（数据缺口）。

### 转股价时间线

初始转股价 + 除权除息事件（派息/送转/配股，同日合并按
`P1 = (P0 − D + A·k) / (1 + n + k)`）+ 下修事件，分段生效。
每个计入日用**当日有效**转股价计算阈值。被撤回的事件不参与计算，
但以 `retracted_events` 列入判断证据。

### 版本与引用

- `data_version`：全局单调递增，任何行情/事件/条款变更都会推进；
- 每次评估生成新的**判断版本**（`version` 递增、`supersedes` 指向前版），
  记录所用 `data_version`、转股价时间线、条款求值顺序、逐日比较明细与数据缺口，
  并附 `evidence_hash`。旧版本完整保留，历史引用可随时复原当时所见；
- 外部结论通过 `POST /references` 登记对某判断版本的引用。同一（债券, 估值日）
  产生新版本时，引用旧版本的结论被标记 `stale` 并附差异摘要——圈出，不覆盖。

### 行情接入语义

- `official_close` / `corrected`：同一日期的新记录使旧记录转为 `superseded`（保留可查）；
- `temporary`：盘中临时价，只存不用；
- `action: "retract"`：撤回某日全部行情，该日转为缺价。
