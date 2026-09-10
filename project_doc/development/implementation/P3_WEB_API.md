---
date: 2026-09-06
purpose: 按当前源码维护 StockManager 的 HTTP API、CLI、输入输出、错误及副作用目录。
project: StockManager
status: active
code_version: 1.17.2
verified_on: 2026-09-06
---

# 当前 API 与命令行手册

适用版本 **1.17.2**，核验日期 2026-09-06，源码基准 `9322845`。沿用原文件名维护，内容覆盖 P3–P5C 的现有实现。旧版首次加载卡片、启动自动回补一年等说明已不适用于当前 Web。

路由事实来源：[WebApp](../../../code/src/stock_manager/web/app.py)、[HTTP 适配器](../../../code/src/stock_manager/web/httpd.py)、[CLI parser](../../../code/src/stock_manager/cli/main.py)。[架构](../architecture/CURRENT_ARCHITECTURE.md) 说明调用关系；[数据能力](../../usage/DATA_CAPABILITIES.md) 说明字段和指标含义。

## 1. 通用约定

- 默认地址 `http://127.0.0.1:8000`；标准库 HTTP 服务，没有 OpenAPI/Swagger 端点。
- JSON 请求使用 `Content-Type: application/json`；应用默认请求体上限 1,000,000 bytes。
- `Decimal` 输出为十进制字符串，日期为 ISO `YYYY-MM-DD`，时间为 ISO datetime，枚举为其 value，缺失值为 `null`。计数和布尔值保持 JSON 类型。
- 本地行情查询不调用 Provider；同步接口可联网，模板和研究接口可写本地文件或 SQLite。`GET /api/sync/pipeline/progress` 可能整理失效 runner/plan 状态，不能视为绝对无写入的审计接口。
- 成功状态包括 200、201、202、204；错误通常为 `{"error":{"code":"BAD_REQUEST","message":"..."}}`。输入合法不等于数据已完整、回测完成或 CAPM 可估计。
- 股票代码使用 Provider 标准化身份，如 `sh.600000`；单日筛选中未知股票返回业务错误。窗口日期必须结合本地日历、覆盖及快照含义判断。

## 2. 完整 HTTP 路由目录

### 页面、规则与筛选

| 方法 | 路径 | 输入 / 输出概要 | 副作用 |
| --- | --- | --- | --- |
| GET | `/` | HTML 数据页、筛选工作台及回测系统 | 无联网 |
| GET | `/styles.css` | 本地 CSS | 无联网 |
| GET | `/app.js` | 本地 JavaScript | 无联网 |
| GET | `/health` | `200 {status:"ok"}` | 无 |
| GET | `/api/version` | `200 {version:"1.17.2"}`，实际值随包版本 | 无 |
| GET | `/api/rules` | `200 {rules:[...]}`，12 条规则及参数元数据 | 无 |
| POST | `/api/screen` | 完整模板、数据集、交易日、复权；同步返回筛选结果 | 本地计算；更新内存进度 |
| GET | `/api/screen/progress` | 最近一次筛选状态、阶段、计数、当前代码 | 读取内存状态 |
| GET | `/api/bars` | 单股最近 bars 或日期区间 | 本地行情读取 |

### 筛选模板

| 方法 | 路径 | 请求 / 返回 | 副作用 |
| --- | --- | --- | --- |
| GET | `/api/templates` | `200 {templates:[{template_id,revision,name,description,is_system}]}` | 本地读取 |
| GET | `/api/templates/{template_id}` | `200 {template:{metadata,rules,composition},is_system}` | 本地读取 |
| POST | `/api/templates/validate` | `{template:...}` → `200 {valid:true,plan:...}` | 编译，不保存 |
| POST | `/api/templates` | `{template:...}` → `201 {template_id,revision}` | 创建 revision 1 用户模板 |
| PUT | `/api/templates/{template_id}` | `{template:...,expected_revision:N}` → `200 {template_id,revision:N+1}` | 覆盖用户模板 |
| DELETE | `/api/templates/{template_id}` | `{expected_revision:N}` → `204` 空响应 | 删除用户模板 |

系统模板不可修改/删除（403）；筛选模板 revision 冲突、重复创建通常为 409 `CONFLICT`。PUT 中模板 ID 必须与 URL 一致。模板 JSON 为 v2，规则参数十进制值用字符串，启用规则必须在组合中恰好引用一次，禁用规则不进入组合；不支持上传任意 Python 代码。

### 同步与本地数据状态

| 方法 | 路径 | 请求 / 返回 | 副作用 |
| --- | --- | --- | --- |
| GET | `/api/sync/status` | 快照候选、覆盖、逐日色块、年度覆盖、active generation、readiness | 本地状态读取 |
| GET | `/api/sync/progress` | `idle/first_run/running/done/error` 等兼容进度 | 读取 Web 内存状态 |
| GET | `/api/sync/backfill/progress` | 旧 v2 回补账本进度 | 本地读取；不等同 P5 当前进度 |
| GET | `/api/sync/pipeline/progress` | `dataset_id=market`（默认）或 `capm`；计划/任务/批次/runner/缺口 | 可能整理失效同步状态 |
| GET | `/api/sync/capm/status` | CAPM generation、覆盖、指数与利率状态、缺口 | 本地读取 |
| POST | `/api/sync/bootstrap` | `source`、`adjustment`、可选 `seed_path` | 在线/增量启动联网 runner；seed 为本地候选流程 |
| POST | `/api/sync/capm-reference` | 可选 `start/end` ISO 日期 | 启动 CAPM 参考数据联网 runner |

`/api/sync/status` 的常用字段：

- `latest_synced_trading_day`、`registered_days`（最多 10 个，倒序，每项只有 `trading_day/source/synced_at`）；generation 在独立字段中，不是每个日期行的字段。
- `recent_days`：30 自然日，状态为 `synced/running/incomplete/missing/failed/nontrading`；`older_bands`：更早 11 个约 30 日段；`year_bands`：年度覆盖。
- `coverage_start/coverage_end` 是此状态视图的近期展示窗口，不能直接当成全部八年数据的 MIN/MAX。没有元数据时字段可能为 null/空列表，`year_bands` 不保证存在。
- `p5_plans`、`active_generation`、`readiness`、`can_enter`；`can_enter=true` 不保证任意历史日期/任意规则的数据齐全。

流水线进度常用字段为 `status/dataset_id/runner/generation/plan_id/mode/target_start/target_end/progress/completed_tasks/total_tasks/task_counts/batch/errors/warnings/gap_details`；无计划时返回 `status:"none"` 的精简对象。成功启动 runner 返回 **200** `{source,runner_pid,note}`，不是已同步成功；重复启动返回 409 `ALREADY_RUNNING`。随后轮询对应数据集的 pipeline 进度。

股票同步体示例（调用会启动同步）：

```json
{"source":"incremental","adjustment":"qfq"}
```

`source` 可为 `online`（默认）、`incremental` 或 `seed`；实现中省略 `adjustment` 默认 `qfq`，调用方建议显式填写。配置需包含同步文件及锁目录。CAPM 同步使用 `{}` 或 `{ "start":"2018-09-03", "end":"2026-09-04" }`；具体日期必须按实际目标选择，默认走配置目标。

**种子分支的当前限制**：`source="seed"` 要求 `seed_path` 及同名 `.manifest.json`，返回 `candidate_id/status/note`，并非已发布 generation。当前处理函数校验外部 seed 文件，但构建候选时绑定的是现有目标库连接，未见复制外部 seed 数据到目标库的步骤；不要将此端点视为已验证的完整外部种子导入。本文未执行种子导入或更改数据库。

### CAPM

| 方法 | 路径 | 请求 / 返回 | 副作用 |
| --- | --- | --- | --- |
| GET | `/api/capm/options` | 必须恰好一个 `as_of`；`200 {as_of,generation_id,defaults,benchmarks,rate_terms}` | 本地参考数据读取 |
| POST | `/api/capm/analyses` | 单股、截至日及回归参数；`201 {analysis_id,results,...实际参数}` | 本地计算并写 `capm_results` |

`benchmarks` 每项含 `index_id/provider_code/name/category/return_version/source/bar_count/coverage_start/coverage_end/coverage_status`；仅列本地已发布选项。`rate_terms` 包含 `term/label/annual_rate/effective_on/source`，年率 `"0.015"` 表示 1.5%；某期限在截至日尚无有效事件时相关字段可为 null。默认选项不是“该数据一定存在”的保证。

```json
{
  "stock_code": "sh.600000",
  "as_of": "2026-09-04",
  "benchmark_id": "hs300.price",
  "rate_term": "1_year",
  "windows": [30, 120, 250, 500],
  "periods_per_year": 252
}
```

`stock_code/as_of` 必填，其余字段默认如示例；`windows` 非空且每项为正整数，年化因子也为正整数，不接受布尔值。未知参数、未发布指数或期限返回 400。API 本身不要求先提交筛选，UI 才限制自动分析通过的选中股票。

每个窗口返回 `window_days/status/reason/estimate`，状态包括 `READY/INELIGIBLE/DATA_INCOMPLETE/NOT_ESTIMABLE`。201 表示分析结果已保存，即使每个窗口都不可估计也可能返回 201。没有对应的 `GET /api/capm/analyses/{analysis_id}` 历史查询路由。

### 策略与回测

| 方法 | 路径 | 请求 / 返回 | 副作用 |
| --- | --- | --- | --- |
| GET | `/api/research/policies` | `200 {policies:{entry:[...],exit:[...],rebalance:[...],allocation:[...],ranking:[...],execution:[...]}}` | 13 个政策定义及参数目录 |
| GET | `/api/research/strategies` | `200 {strategies:[{strategy_template_id,revision,name,description,is_system}]}` | 本地读取 |
| GET | `/api/research/strategies/{strategy_template_id}` | 详情与 `policies`，字段在顶层 | 本地读取 |
| POST | `/api/research/strategies/validate` | `{policies:...}` 或直接 policies → `200 {valid:true,policies:...}` | 规范化，不保存 |
| POST | `/api/research/strategies` | `{strategy_template_id,name,description,policies}` → `201 {strategy_template_id,revision:1}` | 创建用户策略 |
| PUT | `/api/research/strategies/{strategy_template_id}` | `{name,description,policies,expected_revision:N}` → 新 revision | 更新用户策略 |
| DELETE | `/api/research/strategies/{strategy_template_id}` | `{expected_revision:N}` → 204 | 删除用户策略 |
| POST | `/api/research/backtests` | 筛选模板引用、策略和运行设置 → `202 {run_id}` | 提交本地异步研究任务 |
| GET | `/api/research/backtests` | `200 {runs:[...]}`，当前服务默认最多 20 条 | 本地读取；此列表路由未接分页参数 |
| GET | `/api/research/backtests/{run_id}` | 运行状态/进度；有结果时含 `metrics/warnings/settings` | 本地读取 |
| GET | `/api/research/backtests/{run_id}/equity` | `offset/limit` → `{run_id,points,count}` | 本地读取 |
| GET | `/api/research/backtests/{run_id}/orders` | `offset/limit` → `{run_id,orders,count}` | 本地读取 |
| GET | `/api/research/backtests/{run_id}/provenance` | `{run_id,provenance}`；无结果 404 | 本地读取 |
| POST | `/api/research/backtests/{run_id}/cancel` | 无业务请求体 → `{run_id,cancel_requested}` | 请求取消，非立即完成保证 |

策略模板 ID 为小写字母/数字/连字符、最长 64 字符；`name/description` 非空。系统策略只读；策略 revision 冲突为 409 `REVISION_CONFLICT`，与筛选模板的错误码不同。策略 policies 和运行环境分离。

## 3. 关键请求细节

### 单日筛选

必填 `template/dataset_id/trading_day/adjustment`；可选 `codes` 为字符串数组（空数组=该快照股票池），`max_workers` 整数 1–16、默认 4。不接受未列出的字段。

`template` 是完整 v2 模板对象，不是模板 ID。成功返回 200，包括 `template_id/template_revision/dataset_id/trading_day/adjustment/metadata/summary/results/elapsed_seconds/max_workers`。`summary` 为 total/passed/failed；每股有 `code/name/trading_day/passed/rule_executions`。

规则执行项为 `{rule_id,status,result}`，status 为 `PASSED/FAILED/SKIPPED`；禁用规则 SKIPPED 时 result=null。其他 result 有 `passed/actual_value/threshold/reason`。`actual_value` 类型随规则变化，详见数据能力手册。

此接口请求一直执行到筛选结束；`/api/screen/progress` 为 Web 实例共享的最近筛选进度，无 `run_id`，不提供多任务独立进度协议。

### 本地日线

`code/adjustment` 必填；`adjustment` 为 `unadjusted/qfq/hfq`。`end` 可选，省略时取该复权最新 market 元数据日期；不存在则 404。`days` 默认 250、范围 1–500，返回最近至多该数量的 bars。

`start` 可选：提供时读取包含端点的 `[start,end]` 区间并返回 `start` 字段，超过 500 根也可返回；`days` 仍会做参数校验，但不裁剪区间结果。未提供 start 时，读取 `max(days*2,60)` 自然日再截取末尾 days 根，长停牌等情况下不保证取得请求根数。结果按交易日升序；未知股票可能返回 200 和空 bars，不代表自动同步。

### 回测提交

| 字段 | 约束 / 默认 |
| --- | --- |
| `template_id`、`template_revision` | 必填；服务重读已保存筛选模板并比对 revision，即使忽略资格模式也需要有效引用 |
| `strategy_spec_id` 或 `policies` | 至少一种；若同时有 policies，使用自定义政策；策略模板 ID 不能替代这两个字段 |
| `window_years` 或 `backtest_start/backtest_end` | years 为 1–8，存在时优先按本地最新快照及 N×260 交易日回溯；否则两个日期必填 |
| `initial_cash` | 必填，正数，建议十进制字符串 |
| `max_positions` | 默认 20；作为运行级值覆盖分配政策中的同名参数 |
| `max_workers` | 可选 1–16；省略时 Web 注入的服务默认 2，用于资格构建 |
| `codes` | 数组或逗号/空白等分隔字符串；省略为空 |
| `ignore_eligibility` | 布尔值，默认 false；true 时必须指定至少一只股票 |
| `commission_rate/stamp_duty_rate/transfer_fee_rate/min_commission` | 可选非负数；运行级参数，默认由执行适配器取值 |
| `lot_size` | 可选正整数；运行级整手股数 |
| `strategy_template_id/strategy_template_revision` | 可选，用于运行快照来源记录；提交仍需携带实际 policies 或 strategy_spec_id |

入场/退出采用 `{operator:"any"或"all",items:[{policy_id,version,parameters},...]}`，各 1–5 项。回测提交兼容早期单政策格式，保存策略模板应使用分组格式。`rebalance/allocation/ranking/execution` 各一个政策对象；`take_profit_tiers` 最多 5 个 `{take_profit_ratio,partial_ratio}`，按涨幅升序规范化。

净值分页：offset 默认 0、范围 0–100000；limit 默认 500、范围 1–5000。订单 limit 默认 100、其余相同。`count` 是本页条数，不是总条数；应持续翻页到返回数小于 limit。主详情未知 run 返回 404；净值/订单查询没有相同的存在性检查，可返回空列表。

执行状态主要为 `QUEUED → VALIDATING → BUILDING_SIGNALS → RUNNING_BACKTEST → NORMALIZING → SUCCEEDED`，异常/中断/取消分别显式记录。FIFO runner 同时执行一个任务，但不要把它理解为 HTTP 必然拒绝所有第二次提交。取消在执行阶段边界检查，长时间引擎运行不保证立刻退出。

## 4. 本地进程管理

| 方法 | 路径 | 请求 / 返回 | 副作用 |
| --- | --- | --- | --- |
| GET | `/api/instances` | `200 {instances:[...]}` | 检查本机相关进程 |
| POST | `/api/instances/kill` | `{pid:整数}` → `200 {killed:pid}`；必须是识别到的实例 | 向该进程发送 SIGTERM |
| POST | `/api/shutdown` | `{confirm:true}` → `200 {status:"shutting_down"}` | 结束当前 Web 服务 |

## 5. 错误码与状态的解释

| HTTP / code | 常见情形 |
| --- | --- |
| 400 `BAD_REQUEST` | 参数/日期/复权/模板/策略错误；回测 submit 的大多数异常也在此转换 |
| 403 `FORBIDDEN` | 系统模板或权限限制 |
| 404 `NOT_FOUND` | 未知路由、模板、运行、快照或本地数据不足 |
| 409 `CONFLICT` | 筛选模板 revision 或重复创建冲突 |
| 409 `REVISION_CONFLICT` | 策略模板 revision 冲突 |
| 409 `ALREADY_RUNNING` | 同步计划/runner/启动锁表明任务已在运行 |
| 409 `SYNC_COOLDOWN` / `SYNC_RETRY_REQUIRED` | 旧同步异常映射：冷却或需要显式重试 |
| 500 `SYNC_FAILED` / `INTERNAL` | 同步失败或未映射的内部异常 |

未知方法/路径组合可能由分发器返回 404，并非统一 405。HTTP 接收成功、runner 已启动、计划发布成功、数据全部覆盖、回归可估计是不同状态，客户端应分别处理。

## 6. CLI 目录

以下 15 个子命令来自 `_build_parser()`；在 `code/` 下用 `.venv/bin/python -m stock_manager.cli <命令> --help` 查看精确参数，Windows 使用 `.venv\Scripts\python.exe`。

| 子命令 | 主要必填参数 | 用途 / 副作用 |
| --- | --- | --- |
| `screen` | `--db --rules --date --adjustment` | 旧固定规则配置入口；本地筛选 |
| `screen-template` | `--db --template --date --adjustment` | v2 模板本地筛选；`--code` 可重复，`--format json/summary` |
| `sync` | `--db --config --lock-dir --date --adjustment` | 通过 DataSyncService 联网同步；失败可 `--retry`，分支受配置影响 |
| `sync-plan` | `--db --adjustment --start --end` | 离线规划并持久化 plan/task，不仅是打印 |
| `sync-verify` | `--db --candidate --adjustment --start --end` | 离线候选验证并写验证结果，不等于发布 |
| `sync-start` | `--db --config --lock-dir --adjustment --start --end` | 规划并执行 P5 同步，可联网 |
| `sync-retry` | `--db --config --lock-dir --plan-id` | 显式重试 P5 计划，可联网、受冷却约束 |
| `sync-status` | `--db` | 计划/任务/候选/active generation 状态，可选 `--plan-id` |
| `sync-clean` | `--db` | 清理失效运行状态，可选 `--plan-id`；会写库 |
| `sync-import-legacy` | `--db --adjustment --date` | 为旧共享表创建候选，写库 |
| `db-prepare-transfer` | `--db --out` | checkpoint WAL 并生成迁移清单；有写入 |
| `db-verify-transfer` | `--db --manifest` | 核验迁移文件与清单 |
| `status` | `--db --lock-dir --adjustment` | 旧同步状态和锁信息 |
| `smoke` | `--config --code --date --adjustment` | 有界真实 Provider 检查，会联网 |
| `web` | `--db` | 启动 HTTP 服务；建库/幂等迁移及遗留研究状态恢复；仅启动不联网回补 |

当前没有 `capm`、`backtest`、`history-screen` 这样的独立 CLI 子命令；相应能力通过 HTTP 或 Python 服务调用。即使某些命令的业务是读取，构造 `SQLiteRepository` 仍可能建表/迁移。

## 7. 可复用的本地请求示例

以下 Python 示例先读取本地目录并编译模板，再选择真实登记日期；提交筛选会计算但不联网同步。需先启动服务且有可用本地快照。无需手写省略号模板。

```python
import json
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8000"

def request_json(path: str, body: object | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=300) as response:
        return json.load(response)

templates = request_json("/api/templates")["templates"]
if not templates:
    raise RuntimeError("本地没有筛选模板")
template_id = templates[0]["template_id"]
template = request_json("/api/templates/" + template_id)["template"]
request_json("/api/templates/validate", {"template": template})
registered = request_json("/api/sync/status")["registered_days"]
if not registered:
    raise RuntimeError("尚无已登记快照，请先在数据页完成同步")
result = request_json("/api/screen", {
    "template": template, "dataset_id": "market",
    "trading_day": registered[0]["trading_day"],
    "adjustment": "qfq", "codes": [], "max_workers": 1,
})
print(result["summary"])
```

源码引用、JSON 示例、注册目录与部分接口的离线核验记录见 [维护计划](../plan/PB_DOCANDMAINTANENCE.md)。这不是实网同步、真实八年回测或所有路由全量测试的验收报告。
