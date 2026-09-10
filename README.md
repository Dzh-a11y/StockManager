---
date: 2026-09-06
purpose: 根据当前源码说明 StockManager 的功能、启动入口及架构、API、数据能力文档导航。
project: StockManager
status: active
code_version: 1.17.2
verified_on: 2026-09-06
---

# StockManager

StockManager 是运行在本机的 A 股研究型筛选平台，支持参数化选股、历史筛选、策略回测和个股 CAPM 分析。市场数据由 Baostock 同步到本地 SQLite，研究计算使用本地数据。筛选和回测结果仅供研究参考，不构成投资建议；项目不提供自动交易功能。

本文按 **1.17.2** 源码核对，代码基准提交为 `9322845`（2026-09-06 核验），不代表远端发布状态。版本以 [pyproject.toml](code/pyproject.toml)、[包版本](code/src/stock_manager/__init__.py) 和 [版本断言](code/tests/test_package_structure.py) 为准。文档日期与适用版本须同时阅读，历史验收记录不代表当前版本重新通过验收。

## 当前可以做什么

| 能力 | 当前实现与入口 |
| --- | --- |
| 数据初始化与维护 | Web 数据页分别同步股票市场数据和 CAPM 参考数据；在线回补、增量同步、计划/批次/校验与发布状态落库；种子候选接口的限制见 API 手册 |
| 单日筛选 | 12 条内置规则；JSON v2 模板、参数校验、`all` / `any` 组合；逐股返回通过状态、实际值、阈值和原因 |
| 快照日期选择 | UI 从本地 `dataset_metadata` 取最近 10 个已登记的 `market/qfq` 快照，默认最新；仅有日线的日期不等于单日筛选快照 |
| 个股研究 | 本地日 K、成交量和规则命中日期；CAPM 默认输出 30/120/250/500 自然日窗口的 α、年化 α、β、R² 和有效样本数 |
| 历史筛选与回测 | 独立「回测系统」页面，Backtrader 适配器、资格缓存、异步运行、取消、历史结果、净值与成交复盘；支持 1–8 年窗口或指定起止日期 |
| 策略编辑 | 13 个政策定义，分属入场、退出、再平衡、分配、排名和执行六类；入场/退出各 1–5 项，可选 AND/OR；最多 5 档止盈；策略模板支持保存与 revision 校验 |
| 本地 API / CLI | 标准库 HTTP 服务与 `stock-manager` CLI；完整路由、参数、返回值与副作用见接口手册 |

Web 启动只检查本地状态，**当前不会自动发起网络回补**。在数据页点击同步按钮才启动独立同步进程；目标窗口按交易日历及截止时间计算。当前同步配置目标为八年、截止时间为上海时间 17:30，实际数据覆盖以本地状态为准，不能由目标年数推断已完整入库。

股票研究的已选口径为前复权 `qfq`；指数另带价格/全收益版本标记。规则与行情接口要求显式复权，不在缺少数据时联网补取。CAPM、回测会保存研究结果，因此“本地优先”不表示整个应用没有写操作。

## 启动

需要 Python 3.11+。以下命令从项目根目录执行：

```bash
cd code
python3 scripts/launcher.py start
```

启动器会检查/创建 `.venv`，缺少依赖时执行 editable 安装，然后打开 [本地工作台](http://127.0.0.1:8000)。后续可使用 `python3 scripts/launcher.py status`、`restart` 或 `stop`。

- macOS：双击 [StockManager.command](code/scripts/StockManager.command)。
- Windows：双击 [StockManager.bat](code/scripts/StockManager.bat)。
- 安装、桌面入口、打包与排障详见 [使用手册](project_doc/usage/README.md)。本轮未进行全新环境安装或 Windows 实机验收。

已有环境可直接启动本地 Web：

```bash
cd code
.venv/bin/python -m stock_manager.cli web \
  --db data/market.sqlite3 \
  --system-templates config/rule_templates \
  --user-templates data/user-templates \
  --static src/stock_manager/web/static \
  --sync-config config/sync.json \
  --lock-dir data/locks \
  --host 127.0.0.1 --port 8000
```

Windows 对应解释器为 `.venv\Scripts\python.exe`。首次启动允许本地数据库不存在，应用会初始化 schema 并显示数据页，数据就绪后再选择快照进入筛选。

## 文档导航

| 要了解的问题 | 文档 |
| --- | --- |
| 模块职责、调用链、存储边界、源码和测试入口 | [当前架构](project_doc/development/architecture/CURRENT_ARCHITECTURE.md) |
| HTTP API、CLI、请求示例、错误与副作用 | [当前 API 手册](project_doc/development/implementation/P3_WEB_API.md) |
| 已存字段、12 条规则公式、策略计算、CAPM、回测指标及缺口 | [数据与计算能力](project_doc/usage/DATA_CAPABILITIES.md) |
| 页面操作、同步、选股、回测 | [使用手册](project_doc/usage/README.md) |
| 历史计划、ADR、实现记录与文档目录 | [项目文档索引](project_doc/README.md) |
| 开发和文档维护约束 | [AGENTS.md](AGENTS.md) |

当前架构手册单列了源码与旧说明尚未统一的项目；例如固定持有期按自然日计算、排名实现取成交量，以及部分回测指标未填值。使用指标前请阅读数据能力手册中的精确口径。

## 目录与验证

```text
StockManager/                 # Git 根目录
├── README.md                 # 项目入口
├── AGENTS.md                 # 开发规定
├── code/                     # Python 工程；下列路径均相对于此目录
│   ├── pyproject.toml
│   ├── src/stock_manager/    # 应用源码
│   ├── config/               # 同步配置、规则配置与系统模板
│   ├── scripts/              # 启动、同步 runner、维护与基准工具
│   ├── tests/                # 离线测试和 fixtures
│   └── data/                 # 本地数据库、用户模板、日志与锁等运行数据
└── project_doc/              # Obsidian 文档库；技术正文的唯一维护位置
```

根 README 保留项目入口，技术正文在 `project_doc` 维护，不在 `code` 复制 Markdown 手册。日常开发先读文档索引及对应模块文档，再核对源码版本。

```bash
cd code
.venv/bin/python -m pytest
```

这是开发验证命令，不是本轮全量测试通过的声明。本轮为文档整理；源码核对、接口与目录离线抽查的实际验证记录见 [维护计划](project_doc/development/plan/PB_DOCANDMAINTANENCE.md)。
