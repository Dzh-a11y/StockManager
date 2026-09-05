---
date: 2026-09-04
purpose: 记录 StockManager P3 本地 Web 工作台的离线验收结果与浏览器交互验收记录。
project: StockManager
status: accepted
---

# P3 本地 Web 工作台验收记录

## 启动非阻塞化：取消「检查本地数据」加载关卡（2026-09-04）

- 背景与根因：加载卡片第 3 步阻塞等待 `GET /api/sync/status`；在真实库（`code/data/market.sqlite3`，约 2.1 GB、日线千万行）上实测该请求纯 SQL 约 7.3 秒（多次全表索引扫描：无 `(adjustment, trading_day)` 可用索引、单请求重复扫描 20+ 次），成为启动瓶颈。
- 已确认取舍：**不改后端、不加索引、不改 Schema**（避免影响用户已部署数据库）；采用纯前端方案把检查移出启动关键路径。
- 实现（仅前端三件，`code/src/stock_manager/web/static/`）：
  - `index.html`：移除 `#startup-view` 加载卡片；`#gate-view`（数据初始化与同步页）默认可见并带“数据状态：正在检查本地数据…”占位文案。
  - `app.js`：`init()` 不再等待 `/api/sync/status`；`state.uiView` 缺省 `gate`，首屏即数据页，不再按就绪度自动跳转工作台；新增 `prepareWorkspaceResources()`（后台预取规则/模板/研究策略，失败允许重试）、`markServerOk()/markServerPartial()`（顶栏徽标）；进入筛选工作台前先等待资源就绪。
  - `styles.css`：移除 `.startup*` 样式。
- Python 回归：在 `code/` 执行 `.venv/bin/python -m pytest -q`，**724 passed, 11 warnings in 11.90s**。警告为既有模拟 Provider 场景；测试使用本地 fixtures，不访问上游数据源。
- 前端行为：`node --test code/tests/frontend/startup.test.cjs code/tests/frontend/stock-detail.test.cjs`，**20 passed**（startup 6 项 + stock-detail 14 项），只使用 Node 内置测试工具。`startup.test.cjs` 已按新契约重写并覆盖：首屏即数据页且无加载遮罩、状态慢不阻塞首屏、READY 不自动跳转、状态/资源失败不清空界面且无整页重试、资源失败后可重试再进入工作台。
- 说明：数据页色块与覆盖信息仍在首次请求 `/api/sync/status` 的耗时（后台异步填充，界面可先交互）；后端单次聚合与缓存优化未纳入本次范围（决策待定）。
- 人工浏览器走查未执行（需本地启动验证）；使用手册 `usage/README.md` 已同步新的页面加载与首屏说明。

## Windows CI 进程列表测试修复（2026-09-02）

- 问题证据：[CI run 33649852072](https://github.com/Dzh-a11y/StockManager/actions/runs/33649852072) 中 Windows Python 3.11、3.14 均在 `test_instances_endpoint_lists_backfill_runner` 失败，其他平台及 `build-exe` 成功。
- 根因：测试固定模拟 Unix `ps` 文本；Windows 实现使用 PowerShell JSON，解析该文本失败后返回空列表。故障位于测试 fixture 的平台假设。
- 修复：在任一宿主平台显式测试 `posix` 与 `nt` 分支，分别提供 `ps` 文本和 PowerShell JSON；验证 runner/Web 进程、PID、`is_self`、Windows 单对象返回与空列表。只替换 Web 模块的 `os` 引用，不修改全局 `os.name`，避免影响 `Path` 与 pytest。空列表和未知 PID 测试使用固定输入，不再枚举宿主真实进程，并断言未知 PID 不发送终止信号。
- 复现与本地验收：修复前在 macOS 强制执行 `nt` 分支，得到 **1 failed, 1 passed**；修复后相关 **6 passed**，完整离线套件 **604 passed, 5 warnings in 8.10s**。
- 修改范围为测试与本验收文档，运行时代码、接口、页面加载流程均未改变，包版本保持 1.13.10。Windows 实机执行结果以 GitHub Actions 中该修复提交的 CI 记录为准。

## 1.13.10 首次加载进度验收（2026-09-02）

- 已实现：中央加载卡片、五个真实步骤、已等待秒数、10 秒慢提示、60 秒手动重新加载、持续可见的失败原因、成功立即进入原有页面。计划见 `../plan/PHASE_3_PLAN.md` 的已确认扩展。
- Python 回归：在 `code/` 执行 `.venv/bin/python -m pytest -q`，**601 passed, 5 warnings in 8.19s**。警告来自既有模拟 Provider 上市日期缺失和重复同步跳过场景；测试使用本地 fixtures，不访问上游数据源。现有启动器测试需本地回环端口权限。
- 前端行为：在项目根执行 `node --test code/tests/frontend/startup.test.cjs`，**11 passed**。只使用 Node 内置测试工具，不需 npm 安装；CI 增加独立前端测试步骤。测试先出现失败，再完成实现并通过。
- 覆盖：初始 HTML 可见、并行响应顺序变化、10/60 秒边界、无自动重试、手动重新加载、错误持久化和迟到响应、计时器清理、空模板目录、READY/未就绪分流、各必需目录请求失败、默认模板详情失败，以及渲染中途失败时隐藏未完成工作台。
- 真实浏览器：使用独立临时测试数据库与回环 HTTP 服务，模拟本地数据检查延迟 18 秒。观察到 3/5 项、13 秒慢加载提示、完成后自动显示原有数据初始化页及 v1.13.10；模拟 503 后显示阶段、原因与可点击的重新加载按钮，刷新后可再次呈现错误。选中测试数据版本后可正常进入工作台，默认系统模板、规则参数与六类策略完整显示，页面切换不重复显示加载卡片。未操作真实行情库或触发同步。
- 视觉检查：默认桌面窗口与窄屏完成查看；窄屏加载卡片在页面可视宽度内，无横向溢出。进度条有无障碍名称，步骤含文字状态，减少动态效果偏好下停用脉冲动画。60 秒边界由假时钟离线测试验证，未在浏览器中等待完整 60 秒。
- 边界：本次改善反馈，不承诺缩短数据库检查耗时；CI 配置已更新，但尚未推送远端执行。

## P3 初次验收的全量测试（历史记录）

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
