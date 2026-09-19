# 可转债条款分析服务

面向可转债条款、行情与观察窗口的 Python 后端服务：把转股价调整、下修、强赎、
回售、到期与停牌规则转成可版本化的计算过程，支持逐日复核。

- 滚动窗口只计入**适用交易日**；停牌、缺价、公告撤回、盘中临时价分类处理并留痕；
- 行情修订/补发后重新评估生成**新判断版本**，引用旧版本的结论被圈出，不覆盖历史；
- 条款互相改变转股价或观察期时按依赖拓扑排序求值，循环依赖直接阻止；
- 输入任一估值日即可取得：距触发还差哪些日期、逐日价格比较、当前有效条款与数据缺口。

## 运行

需要 Python 3.11 或更高版本（仅标准库，无第三方依赖）：

```bash
python3 src/index.py
```

服务默认监听 `8000` 端口。首次启动时若存储为空，会自动装载 `reference/` 下的
样例（债券条款、正股行情、除权事件、交易日历）。持久化文件写入 `.runtime/`
（可用环境变量 `CB_DATA_DIR` 覆盖）。

执行测试与情景演示：

```bash
python3 -m unittest discover -s tests
python3 scripts/demo_morning_meeting.py   # 晨会故事线：补发行情 -> 新版本 -> 圈出旧引用
```

也可以运行 `docker compose up --build` 启动容器。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/bonds` | 债券列表 |
| POST | `/bonds` | 登记债券与条款（循环依赖返回 409 与环路径） |
| GET | `/bonds/{id}` | 债券详情 + 判断版本摘要（含 `data_stale` 提示） |
| POST | `/bonds/{id}/prices` | 接入行情：`official_close` / `corrected` / `temporary` / `action=retract` |
| POST | `/bonds/{id}/events` | 接入事件：除权、下修、停牌、`retraction`（公告撤回） |
| POST | `/bonds/{id}/evaluate` | 按估值日评估，生成新判断版本（请求体 `{"date": "YYYY-MM-DD"}`） |
| GET | `/bonds/{id}/judgments` | 判断版本列表（`?valuation_date=` 过滤） |
| GET | `/bonds/{id}/gaps` | 数据缺口扫描（`?date=`） |
| GET | `/judgments/{jid}` | 任一历史判断版本的完整证据 |
| POST | `/references` | 登记外部结论对判断版本的引用 |
| GET | `/references` | 引用列表（`?status=stale` 查看被圈出的） |

错误响应统一为 `{"error": {"code", "message"}}`。

## 判断版本内容

每次 `evaluate` 返回并持久化一个判断版本，关键字段：

- `effective_conversion_price` / `conversion_price_timeline`：估值日有效转股价及完整时间线；
- `dependency_order`：本次条款求值顺序（拓扑排序结果）；
- `clauses[].state`：`condition_met` / `counting` / `accumulating` / `not_applicable` / `expired`；
- `clauses[].window`：窗口起止、计入天数、达标天数、观察期重起点；
- `clauses[].remaining`：`hits_needed`、`needed_dates`、`earliest_trigger_date`
  （假设未来每个适用交易日均达标）；
- `clauses[].days`：逐日 `close` vs `threshold` 比较、价差、行情来源与修订号；
- `clauses[].excluded_days` / `gaps`：停牌、缺价、临时价、撤价的逐日分类；
- `changes_from_previous`：与上一判断版本的可读差异；
- `evidence_hash`：证据内容哈希，供完整性校验。

## 目录

```
src/cb/            领域核心：模型、日历、定价、依赖排序、评估引擎、接入、存储
src/app.py         HTTP 接口层
reference/         样例：债券条款、行情（含补发批次）、事件、交易日历、公开枚举
scripts/           晨会情景演示
tests/             单元与端到端测试
docs/domain.md     领域语义说明
```
