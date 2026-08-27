---
date: 2026-08-25
purpose: 冻结 StockManager P3 本地 Web 可视化与规则自动扩展的 HTTP 边界、JSON 契约和安全边界。
project: StockManager
status: accepted
---

# ADR：P3 本地 Web 可视化与规则自动扩展契约

## 状态

已接受并实现。

## 背景

P2 已提供参数化规则后端、模板编译器与服务层。P3 在其上增加一个本地、离线优先的 Web 操作界面，让用户可以加载模板、开关规则、修改参数、调整组合、保存用户模板并运行筛选。P3 的核心目标是建立元数据驱动链路：新增可信规则后，后端自动暴露规则目录，前端根据参数定义自动生成控件，普通新规则不要求修改筛选主循环、HTTP 路由或前端规则清单。

## 决策

### 技术选型

- 使用 Python 标准库 `http.server` 与 `ThreadingHTTPServer`，不引入 Web 框架、构建链或外部 CDN。
- 前端使用纯 HTML、CSS、JavaScript，无框架、无构建产物。
- 依赖保持 P2 基线（`baostock`、`pytest`），不新增运行时依赖。
- 本机默认只监听 `127.0.0.1`，端口可配置。

### 分层边界

```text
HTML / CSS / JavaScript
  -> Local HTTP API
  -> TemplateService / TemplateCompiler
  -> ParameterizedScreeningService
  -> LocalRepositoryProtocol
  -> SQLite

BaostockProvider
  -> DataSyncService
  -> SQLite
```

- Web 模块不得导入 `BaostockProvider` 或 `DataSyncService`。
- Web 启动时由受控配置提供 SQLite 数据库路径、系统模板根目录与用户模板根目录；浏览器不能决定任何文件路径。
- API 层只负责解析、校验、状态码与 JSON 格式化，不实现筛选公式。
- 模板必须经过 `parse_template` 与 `TemplateCompiler`，前端校验不能替代后端校验。

### 静态资源白名单

仅允许以下路径：

- `GET /`：筛选工作台页面。
- `GET /styles.css`：本地样式。
- `GET /app.js`：本地交互逻辑。

不接受任意文件路径，未知静态路径返回结构化 404。

### JSON 表示

- `Decimal` 序列化为十进制字符串（如 `"6.5"`），不使用浮点，保证精度与确定性。
- `Enum` 序列化为 `value`。
- `date` 与 `datetime` 序列化为 ISO 8601 字符串；`datetime` 保留 UTC 偏移字符串。
- `dataclass` 递归序列化为字典；`tuple`/`list` 序列化为数组；字典键统一为字符串。
- `None`、`bool`、`int`、`str` 原样输出；`float` 仅在非关键路径出现。

### 拟定接口

最终契约以本 ADR 与契约测试为准。

#### 页面与静态资源

- `GET /`：筛选工作台。
- `GET /styles.css`：本地样式。
- `GET /app.js`：本地交互逻辑。
- `GET /health`：健康检查，返回 `{"status":"ok"}` 与 200。

#### 规则目录

- `GET /api/rules`：返回所有已注册 `RuleDefinition`。
- 响应结构：`{"rules": [RuleEntry, ...]}`。
- `RuleEntry` 至少包含 `rule_id`、`name`、`description`、`parameters`。
- `ParameterEntry` 至少包含 `parameter_id`、`value_type`、`required`、`default_value`、`minimum`、`maximum`、`label`、`description`。

#### 模板

- `GET /api/templates`：模板摘要列表，`{"templates": [TemplateSummary, ...]}`。
  - `TemplateSummary`：`template_id`、`revision`、`name`、`description`、`is_system`。
- `GET /api/templates/{template_id}`：完整模板，`{"template": {...}, "is_system": bool}`。
  - `template` 即版本 2 模板对象（`metadata`、`rules`、`composition`）。`is_system` 与模板载荷分离，避免污染模板 JSON。
- `POST /api/templates/validate`：解析并编译临时模板，不保存。
  - 请求体：`{"template": {...}}`。
  - 成功：`200`，返回 `{"valid": true, "plan": {...}}`。
  - 失败：`400`，返回结构化错误。
- `POST /api/templates`：创建 revision 1 用户模板。请求体：`{"template": {...}}`。
  - 成功：`201`，返回 `{"template_id": ..., "revision": 1}`。
  - 重复：`409`。
- `PUT /api/templates/{template_id}`：携带 `expected_revision` 更新。请求体：`{"template": {...}, "expected_revision": N}`。
  - 成功：`200`，返回 `{"template_id": ..., "revision": N+1}`。
  - 冲突：`409`。
- `DELETE /api/templates/{template_id}`：请求体：`{"expected_revision": N}`。
  - 成功：`204`。
  - 冲突：`409`。

#### 筛选

- `POST /api/screen`。
  - 请求体：`{"template": {...}, "dataset_id": str, "trading_day": "YYYY-MM-DD", "adjustment": "qfq", "codes": [str, ...]?}`。
  - 响应：
    ```json
    {
      "template_id": "...",
      "template_revision": 1,
      "dataset_id": "...",
      "trading_day": "YYYY-MM-DD",
      "adjustment": "qfq",
      "metadata": {...DatasetMetadata...},
      "summary": {"total": N, "passed": P, "failed": F},
      "results": [
        {
          "code": "...",
          "name": "...",
          "trading_day": "YYYY-MM-DD",
          "passed": true,
          "rule_executions": [
            {
              "rule_id": "...",
              "status": "PASSED",
              "result": {
                "passed": true,
                "actual_value": "...",
                "threshold": "...",
                "reason": "..."
              }
            }
          ]
        }
      ]
    }
    ```
  - `SKIPPED` 规则 `result` 为 `null`。
  - 本地数据不足时返回明确业务错误（`404`），不调用同步服务。

### 错误映射

- `400`：请求结构、模板、参数、日期或复权错误。
- `403`：尝试修改系统模板。
- `404`：模板、静态资源或本地数据不存在。
- `409`：模板重复或 revision 冲突。
- `500`：未预期服务器错误；记录诊断信息但响应不得泄露本机路径或堆栈。
- 错误响应统一为 `{"error": {"code": "HTTP_STATUS_TEXT", "message": "..."}}`，避免泄露内部异常类名。

## 结果

- 页面不硬编码内置规则清单，新注册普通规则可自动发现和渲染。
- 所有筛选请求只读本地 SQLite，断网可运行，Web 路径不持有 Provider。
- 模板解析、参数校验、组合校验和 revision 冲突仍由后端权威处理。
- 系统模板只读，用户模板 CRUD、临时筛选和错误状态均有测试。
- 静态资源只从固定白名单提供。
