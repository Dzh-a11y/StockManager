---
date: 2026-09-03
purpose: 记录 StockManager P3 本地 Web 工作台的启动方式、接口契约与错误映射。
project: StockManager
status: active
---

# P3 本地 Web 工作台：使用与 API 文档

## 首次页面加载（1.13.10）

首次打开与整页刷新时，HTML 直接显示中央加载卡片，原有数据初始化页和工作台保持隐藏。加载规则（`GET /api/rules`）、读取模板（`GET /api/templates`）、加载策略（`GET /api/research/policies`）、检查本地数据（`GET /api/sync/status`）并行执行；随后加载首个系统模板（否则首个模板）的详情并渲染页面，计为“准备工作台”。空模板目录允许完成加载并给出提示。

进度显示真实完成步骤数 N/5，并非耗时百分比。已等待时间使用浏览器单调时钟，每秒更新；10 秒后给出慢加载提示，60 秒后展示“重新加载”，允许继续等待。失败立即保留步骤和原因、停止计时，尚在进行的步骤标记“未完成”；后续响应不能覆盖该错误。重新加载执行整页刷新，无自动重试。

全部完成后立即显示原有页面：数据就绪进入工作台，否则进入数据初始化页。数据未就绪本身不属于页面加载失败。版本、进程信息和后台进度轮询在成功后启动；五个步骤均只调用已有本地读取接口，不触发同步、回补或 Provider 请求。无新增 HTTP 契约和运行时依赖。

## 启动

P3 提供 `stock-manager web` 子命令，默认只监听 `127.0.0.1`。先进入代码仓库并安装依赖：

```bash
cd /Users/douzihao/StockManager/code
python3 -m pip install '.[dev]'

stock-manager web \
  --db data/market.sqlite3 \
  --system-templates config/rule_templates \
  --user-templates data/user-templates \
  --static src/stock_manager/web/static \
  --host 127.0.0.1 \
  --port 8000
```

启动前校验 SQLite 数据库存在，不会为筛选偷偷创建空库。浏览器打开 `http://127.0.0.1:8000` 进入筛选工作台。

## 页面信息架构

- **运行条件**：数据集、交易日、复权方式、可选股票代码。
- **策略编辑器**：模板选择、规则卡片、参数控件、规则开关、分组组合 `all`/`any`。
- **筛选结果**：总数/通过/失败统计、可点击或键盘选择的结果表。
- **独立个股研究（1.15.2）**：位于数据同步与维护上方，按符合条件、日 K 与成交量、CAPM 三列排列；窄屏顺序堆叠。下方配置市场指数、已发布利率期限与年化因子，提供公式和模型假设说明。K 线信息栏在「涨幅次数」规则（`limit_up_3m`）启用时附加其返回的涨幅日期（`actual_value.trading_days`）。
- **折叠（1.15.2）**：主工作台八个模块均有原生按钮，通过 `aria-expanded`、`aria-controls` 与内容 `hidden` 同步控制；不卸载内容，本浏览器记住折叠状态。

参数控件由 `GET /api/rules` 元数据自动生成，页面不硬编码内置规则清单。

## 接口

### CAPM 本地选项与分析（1.15.2）

`GET /api/capm/options?as_of=YYYY-MM-DD` 要求单个规范 ISO 日期，未知/重复参数返回 400。一个 SQLite 只读快照返回 `as_of`、`generation_id`、`defaults`、`benchmarks`、`rate_terms`；未发布时 generation 为 null、列表为空，不触发同步。

- `defaults`：`benchmark_id="hs300.price"`、`rate_term="1_year"`、`periods_per_year=252`。
- `benchmarks`：每项包含 `index_id/name/provider_code/return_version/category/source/bar_count/coverage_start/coverage_end/coverage_status`，仅来自 active generation 的 manifest。覆盖统计截至请求日期，不保证每个回归窗口可算。
- `rate_terms`：每项为 `term/label/annual_rate/effective_on/source`；年率为十进制小数字符串（`"0.015"` 表示 1.5%），取截至日最新有效事件。只有未来记录的期限，其年率/生效日/来源为 null，UI 不允许用于该日期。当前 Provider 仅保存一年期。

`POST /api/capm/analyses` 接受 `stock_code/as_of`，以及可选 `benchmark_id/rate_term/windows/periods_per_year`。默认窗口 `[30,120,250,500]`；窗口列表不得为空，各窗口及年化因子须为正整数，不接受布尔值。未知/未发布指数或利率期限返回 400，不运行分析或保存。

成功返回 201，包含既有 `analysis_id/results`，以及实际提交的 `stock_code/as_of/benchmark_id/benchmark_return_version/rate_term/periods_per_year`。每个窗口结果为 `window_days/status/reason/estimate`；`estimate` 含日 alpha、线性年化 alpha、beta、R²、有效收益观察数、年化因子。输入不足是结构化窗口状态；不因选项可见就把缺行情视为可估计。该接口仅本地读取和保存分析，历史利率继续按生效日分段。

前端设置为浏览器级共享偏好，不是服务器项目设置 API；切换模板不会覆盖它。缓存包含分析参数、日期及参考 generation；选股/新筛选/版本变化的迟到响应被丢弃，股票版本变化要求重新筛选。CAPM 结果更新不重建图表。

### 页面与静态资源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 筛选工作台页面 |
| GET | `/styles.css` | 本地样式 |
| GET | `/app.js` | 本地交互逻辑 |
| GET | `/health` | 健康检查，返回 `{"status":"ok"}` |

静态资源只从固定白名单提供，未知路径返回结构化 `404`。

### 规则目录

`GET /api/rules` 返回所有已注册 `RuleDefinition`：

```json
{
  "rules": [
    {
      "rule_id": "pe_positive",
      "name": "PE 下限",
      "description": "PE TTM 必须存在并严格大于下限",
      "parameters": [
        {
          "parameter_id": "minimum_exclusive",
          "value_type": "decimal",
          "required": true,
          "default_value": "0",
          "minimum": null,
          "maximum": null,
          "label": "PE 严格下限",
          "description": "PE 严格下限"
        }
      ]
    }
  ]
}
```

### 模板

- `GET /api/templates`：摘要列表，`{"templates":[TemplateSummary,...]}`。每条含 `template_id`、`revision`、`name`、`description`、`is_system`。
- `GET /api/templates/{template_id}`：完整模板，`{"template":{...},"is_system":bool}`。
- `POST /api/templates/validate`：请求体 `{"template":{...}}`，解析并编译临时模板，不保存；成功 `200`，失败 `400`。
- `POST /api/templates`：创建 revision 1 用户模板；成功 `201`，重复 `409`。
- `PUT /api/templates/{template_id}`：请求体 `{"template":{...},"expected_revision":N}`；成功 `200` 返回 `{"template_id":...,"revision":N+1}`，冲突 `409`。
- `DELETE /api/templates/{template_id}`：请求体 `{"expected_revision":N}`；成功 `204`，冲突 `409`。

系统模板只读，修改、删除系统模板返回 `403`。

### 筛选

`POST /api/screen`：

```json
{
  "template": {"metadata": {...}, "rules": {...}, "composition": {...}},
  "dataset_id": "market",
  "trading_day": "2026-08-25",
  "adjustment": "qfq",
  "codes": ["sh.600001"],
  "max_workers": 4
}

`max_workers`（可选，整数 1~16，默认 4）：筛选并发 worker 数。worker 越多越快，但内存与 CPU 占用越高；小数据量时收益不明显。超出 1~16 或非整数返回 `400`。
```

响应：

```json
{
  "template_id": "system-default",
  "template_revision": 1,
  "dataset_id": "market",
  "trading_day": "2026-08-25",
  "adjustment": "qfq",
  "max_workers": 4,
  "elapsed_seconds": 1.234,
  "metadata": {...},
  "summary": {"total": 1, "passed": 1, "failed": 0},
  "results": [
    {
      "code": "sh.600001",
      "name": "Alpha",
      "trading_day": "2026-08-25",
      "passed": true,
      "rule_executions": [
        {"rule_id": "non_st", "status": "PASSED", "result": {"passed": true, "actual_value": "...", "threshold": "...", "reason": "..."}}
      ]
    }
  ]
}
```

`SKIPPED` 规则的 `result` 为 `null`。筛选只读本地 SQLite，断网可运行；本地数据不足时返回业务错误（`404`），不调用同步服务。

### 本地日K数据

`GET /api/bars` 返回单只股票最近的本地日K线（含成交量），供筛选结果中点击个股后绘制 K 线图：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `code` | 是 | 股票代码，如 `sh.600001` |
| `adjustment` | 是 | 复权方式：`unadjusted` / `qfq` / `hfq` |
| `end` | 否 | 截止交易日（ISO 日期）；缺省为最新已同步交易日 |
| `days` | 否 | 返回最近多少根日K，范围 1..500，缺省 250 |

响应：

```json
{
  "code": "sh.600001",
  "adjustment": "qfq",
  "end": "2026-08-25",
  "bars": [
    {"code": "sh.600001", "trading_day": "2026-08-24", "open": "100", "high": "109", "low": "99", "close": "109", "preclose": "100", "volume": "400", "amount": "1000", "is_trading": true}
  ]
}
```

`bars` 按交易日升序，最多返回 `days` 根；`Decimal` 字段序列化为十进制字符串。接口只读本地 SQLite，不访问 Provider；指定复权方式没有本地数据集（且未提供 `end`）时返回 `404`；参数缺失或非法返回 `400`；本地无该股票数据时返回 `200` 与空 `bars`。

### 数据回补（启动自动）与数据状态

手动同步入口已移除（软件不常驻电脑，数据由 Web 启动时的自动回补写入，或在需要时用 `stock-manager sync` CLI 手动执行）。Web 启动时若配置了同步（`--sync-config`、`--lock-dir`），会自动回补一年数据并做增量 tail；不配置同步则筛选只读取本地已有数据。

`GET /api/sync/progress`：返回自动回补进度，`{"status":"idle|running|done|error","dataset_id":...,"trading_day":...,"phase":"daily_bars|fundamentals|dividends|starting","batch_phase":...,"batch_completed":N,"batch_total":N,"completed":N,"total":N,"current_code":...,"message":...}`，前端据此显示进度条与当前正在加载的股票。

`GET /api/version`：返回当前包版本，`{"version": "1.6.0"}`，前端在顶部显示 `v1.6.0`。

`GET /api/sync/status`：返回本地数据覆盖概览，用于界面上的"数据状态"卡片：

```json
{
  "latest_synced_trading_day": "2026-08-25",
  "coverage_start": "2025-08-31",
  "coverage_end": "2026-08-25",
  "stocks_count": 2,
  "recent_days": [{"day": "2026-08-25", "status": "synced"}, ...],
  "older_bands": [{"start": "2026-06-27", "end": "2026-07-26", "coverage": 0.0, "incomplete": false}, ...]
}
```

`recent_days` 给出最近 30 个自然日逐日的状态（`synced`/`running`/`incomplete`/`missing`/`failed`/`nontrading`），前端据此渲染逐日色块：`synced`=绿、`running`=Orange（拉取中）、`incomplete`=Orange（未完全同步）、`failed`=红、`missing`=灰、`nontrading`=浅。日常判定以该日 `sync_record` 的实际状态为准；对无记录的历史交易日，若当日 bar 的不同股票数 < 股票池规模的 95%（如首次启动只同步了一部分），则视为 `incomplete`（未完全同步，橙色），否则为 `synced`。`older_bands` 给出近 360 天窗口内更早的 11 段（每段约 30 天）的覆盖率，`incomplete` 表示该段存在未完全同步的天。最新交易日不存在时返回全 `null`/空数组。

`GET /api/screen/progress`：返回筛选进度，`{"status":"idle|running|done|error","phase":"screening","done":N,"total":N,"current_code":...,"message":...}`，前端在 `POST /api/screen` 运行期间据此轮询渲染进度条。

### 停止服务与多实例

`POST /api/shutdown`：请求体 `{"confirm": true}`。确认后向本进程发送 `SIGTERM`（延迟 0.5 秒以确保响应先返回），用于在界面内直接结束服务进程，无需进入终端。未确认（`confirm` 缺失或非 `true`）返回 `400`。停止后需重新执行 `stock-manager web` 启动命令。

`GET /api/instances`：返回本机正在运行的 `stock-manager` 进程列表，`{"instances":[{"pid":N,"command":"...","is_self":true|false}, ...]}`，用于诊断重复实例（避免文件/进程锁的 `Errno36` EDEADLOCK 冲突）。当前服务所在进程标记 `is_self`。

`POST /api/instances/kill`：请求体 `{"pid": N}`。对指定进程发送 `SIGTERM`，用于在界面内直接停止某个重复实例，无需进入终端。进程不存在返回 `404`；发送失败返回 `400`。

## 错误映射

| 状态 | 含义 |
| --- | --- |
| 400 | 请求结构、模板、参数、日期或复权错误 |
| 403 | 尝试修改系统模板 |
| 404 | 模板、静态资源或本地数据不存在 |
| 409 | 模板重复或 revision 冲突 |
| 500 | 未预期服务器错误（响应不含本机路径或堆栈） |

错误响应统一为 `{"error":{"code":"...","message":"..."}}`。

## 本地优先边界

Web 启动时由受控配置提供 SQLite、模板根目录和静态目录；浏览器不能决定任何文件路径。模板必须经过 `parse_template` 与 `TemplateCompiler`，前端校验不能替代后端校验。筛选（`POST /api/screen`）只读本地 SQLite。启动时的自动回补（`backfill_on_startup`）由后端调用 `DataSyncService`，作为唯一允许访问 Baostock 的入口；浏览器不直接访问 Provider，仍遵守单一入口、锁、防重复与速率限制。
