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

Web 模块不导入 `BaostockProvider` 或 `DataSyncService`。Web 启动时由受控配置提供 SQLite、模板根目录和静态目录；浏览器不能决定任何文件路径。模板必须经过 `parse_template` 与 `TemplateCompiler`，前端校验不能替代后端校验。
