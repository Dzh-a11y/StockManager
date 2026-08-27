---
date: 2026-08-25
purpose: 记录 StockManager P3 本地 Web 工作台的启动方式、接口契约与错误映射。
project: StockManager
status: active
---

# P3 本地 Web 工作台：使用与 API 文档

## 启动

P3 提供 `stock-manager web` 子命令，默认只监听 `127.0.0.1`。先进入代码仓库并安装依赖：

```bash
cd /Users/douzihao/StockManager/stock_analysis_based_on_baostock_2025_09_19
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
- **筛选结果**：总数/通过/失败统计、结果表、逐规则详情。

参数控件由 `GET /api/rules` 元数据自动生成，页面不硬编码内置规则清单。

## 接口

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
  "codes": ["sh.600001"]
}
```

响应：

```json
{
  "template_id": "system-default",
  "template_revision": 1,
  "dataset_id": "market",
  "trading_day": "2026-08-25",
  "adjustment": "qfq",
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

`GET /api/sync/status`：返回本地数据覆盖概览，用于界面上的"数据状态"卡片：

```json
{
  "latest_synced_trading_day": "2026-08-25",
  "coverage_start": "2025-08-31",
  "coverage_end": "2026-08-25",
  "stocks_count": 2,
  "recent_days": [{"day": "2026-08-25", "status": "synced"}, ...],
  "older_bands": [{"start": "2026-06-27", "end": "2026-07-26", "coverage": 0.0}, ...]
}
```

`recent_days` 给出最近 30 个自然日逐日的状态（`synced`/`missing`/`failed`/`nontrading`），前端据此渲染逐日色块；`older_bands` 给出近 360 天窗口内更早的 11 段（每段约 30 天）的覆盖率，前端据此按覆盖率着色。最新交易日不存在时返回全 `null`/空数组。

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
