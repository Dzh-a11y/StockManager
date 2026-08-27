---
date: 2026-08-25
purpose: 记录 StockManager P3 本地 Web 工作台的离线验收结果与浏览器交互验收记录。
project: StockManager
status: accepted
---

# P3 本地 Web 工作台验收记录

## 全量测试

执行 `python3 -m pytest`，离线环境下全部通过：

```text
109 passed
```

其中 `tests/test_web_api.py` 覆盖 P3 的 HTTP 契约测试，全部断网运行：
规则目录元数据驱动、静态资源白名单与健康检查、模板列表/详情/校验/创建/更新/删除、revision 冲突、系统模板只读、离线筛选三态结果、本地数据缺失 404、请求体大小限制，以及新规则端到端扩展性。

## 已验证的接口链路

- `GET /api/rules`：输出来自 `RuleRegistry.definitions()`，不含第二份规则清单。
- `GET /api/templates` 与 `GET /api/templates/{id}`：模板摘要与完整载荷，`is_system` 与模板载荷分离。
- `POST /api/templates/validate`：解析并编译临时模板，不保存；非法模板返回 400。
- `POST /api/templates`、`PUT /api/templates/{id}`、`DELETE /api/templates/{id}`：用户模板 CRUD 与乐观并发 revision 冲突（409）。
- 系统模板修改/删除返回 403。
- `POST /api/screen`：返回统计、结果表与逐规则 `PASSED`/`FAILED`/`SKIPPED`；本地数据不足返回业务 404，不调用同步服务。
- `GET /`、`/styles.css`、`/app.js`、`/health`：页面与静态资源白名单正常；未知路由 404。

## 浏览器交互验收

以下流程已通过前端实现并由自动化契约测试覆盖其底层同步调用；实际浏览器交互需在本地启动 `stock-manager web` 后人工走查：

1. 首次加载读取规则目录并默认载入系统模板。
2. 修改参数、开关规则、调整分组与顶层 `all`/`any` 后显示「未保存」脏状态。
3. 「校验」调用后端确认可编译。
4. 「另存为」创建用户模板（revision 1），「保存」携带 `expected_revision` 更新并递增 revision。
5. 系统模板只读，只能另存为；删除仅对用户模板开放并确认。
6. revision 冲突时提示重新加载，不静默覆盖。
7. 运行筛选后结果页显示总数/通过/失败统计、结果表与逐规则详情，区分 `PASSED`/`FAILED`/`SKIPPED`。
8. 空结果、加载中、服务错误均有明确状态；页面与页脚包含研究用途免责声明。

## 边界与安全

- 默认只监听 `127.0.0.1`，端口可配置。
- 启动前校验 SQLite 存在，不创建空库。
- 静态资源只从固定白名单提供，不接受任意文件路径。
- Web 模块不导入 `BaostockProvider` 或 `DataSyncService`。
- 模板必须经过 `parse_template` 与 `TemplateCompiler`，前端校验不替代后端校验。
- `Decimal` 以字符串输出、`Enum` 输出 `value`、日期输出 ISO 8601，保证确定性。
- 错误响应不泄露本机绝对路径或堆栈。

## 免责声明

所有筛选结果仅供研究参考，不构成任何投资建议。项目禁止实现自动交易功能。
