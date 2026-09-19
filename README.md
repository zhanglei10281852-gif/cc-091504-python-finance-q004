# 可转债条款分析服务

面向可转债条款、行情与观察窗口的 Python 后端服务。把转股价调整、下修、强赎、
回售、到期与停牌规则转成可版本化的计算过程：每一次评估都产出带完整证据与
输入清单的**判断版本**，行情修订只追加新版本、圈出引用旧版本的结论，历史
版本可按当时输入精确回放复核。

## 运行

需要 Python 3.11 或更高版本（仅标准库，无第三方依赖）：

```bash
python3 src/index.py
```

服务默认监听 `8000` 端口。执行测试与场景演示：

```bash
python3 -m unittest discover -s tests
python3 scripts/demo.py     # 晨会场景：补发行情 → 新版本 → 圈出旧引用 → 回放
```

也可以运行 `docker compose up --build` 启动容器。

## 快速开始

```bash
curl -X POST localhost:8000/admin/seed          # 载入 reference/ 样例数据
curl -X POST localhost:8000/evaluate \
  -d '{"bond_id": "CB_DEMO", "valuation_date": "2026-09-18"}'
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/admin/seed` | 空库时载入 `reference/*.sample.json` |
| POST | `/bonds` | 登记债券条款（版本化；循环依赖返回 422） |
| GET | `/bonds` / `/bonds/{id}` | 条款列表 / 当前条款 |
| POST | `/quotes` | 摄入行情（对象或数组）；`price_source` 区分 `official_close` / `corrected` / `temporary` |
| POST | `/corporate-actions` | 除权事件（送转/配股/分红/组合）；`status=withdrawn` 表示公告撤回 |
| POST | `/suspensions` | 停牌登记 / 撤回 |
| POST | `/price-resets` | 已批准下修（新转股价与生效日）/ 撤回 |
| POST | `/evaluate` | 评估 `{bond_id, valuation_date}`；内容不变时返回既有版本 |
| GET | `/bonds/{id}/judgments?valuation_date=` | 判断版本列表 |
| GET | `/judgments/{id}` | 某版本的完整证据 |
| POST | `/judgments/{id}/replay` | 按当时输入重算并校验一致性 |
| POST | `/references` | 登记结论引用 `{judgment_id, cited_by}` |
| GET | `/bonds/{id}/references` | 引用列表；过期引用标 `stale` 并附差异摘要 |

摄入行情/公告后，受影响债券的既有估值日会自动重评估；内容有变化才追加
新版本（响应中的 `reevaluated` 列出）。

## 评估结果包含什么

- `dependency_order`：条款评估的确定性顺序（拓扑排序，循环依赖在登记条款时即被拒绝）；
- `effective_terms`：估值日有效转股价、转股价时间线（初始价 → 除权调整 → 下修）、生效条款；
- `clauses[*].days`：每个适用交易日的收盘价、当日转股价、阈值、比较式与命中结果（逐日证据）；
- `clauses[*].excluded`：被排除的交易日及原因——`suspended`（停牌）、`missing_price`（缺价）、
  `temporary_only`（仅盘中临时价）；缺价与临时价同时计入 `data_gaps`；
- `summary` / `trigger` / `projection`：窗口内达标数、是否触发、距离触发尚缺几天、
  假设未来每日达标的最早触发日与候选日期；
- `manifest`：本次计算使用的全部输入版本（条款/日历/行情/除权/停牌/下修）与回放游标。

## 目录

- `src/cb/`：领域内核（`store` 版本化存储、`calendar` 交易日历、`price_schedule`
  转股价时间线、`clauses` 窗口评估与依赖排序、`engine` 评估编排、`service` 摄入与重评估）
- `reference/`：可公开样例（条款、行情、除权、停牌、交易日历）与枚举约定
- `.runtime/`：运行期持久化（追加式 JSONL，不入库）
- `scripts/demo.py`：晨会场景端到端演示
