---
date: 2026-08-25
purpose: 说明 StockManager 的项目定位、架构边界与开发入口。
project: StockManager
status: active
---

# StockManager

StockManager 是面向 A 股的研究型筛选平台，目标是建立可测试、可维护的 Python 筛选内核。所有筛选结果仅供研究参考，不构成任何投资建议。项目严禁实现自动交易功能。

## 架构边界

- 当前外部数据源为 Baostock，但 Provider 层必须保持可替换，业务代码不得硬编码 Baostock 特定逻辑。
- 筛选服务和规则只读取本地 SQLite，不得直接访问外部 Provider。
- 复权方式不设隐式默认值；每个数据集、规则和 CLI 请求都必须显式记录或提供复权方式。当前规则配置明确使用 `qfq`。
- 旧 Python 架构已在迁移验收后移出代码仓库；历史行为证据保留在固定 fixture、迁移文档和项目归档中。

## 开发

先进入包含 `pyproject.toml` 的代码仓库。不要在上一级 `/Users/douzihao/StockManager` 直接执行安装命令：

```bash
cd /Users/douzihao/StockManager/stock_analysis_based_on_baostock_2025_09_19
python3 -m pip install '.[dev]'
python3 -m pytest
```

在 macOS 的 Python 3.14 虚拟环境中不建议使用 `pip install -e`：若 `.venv` 带 Finder 的 hidden 文件标志，Python 会忽略 editable 安装生成的 `.pth`，从而出现“安装成功但无法导入 `stock_manager`”。普通本地安装不依赖该 `.pth`。

P1-1 至 P1-7 已完成：工程骨架、领域协议、旧规则快照、纯规则、本地存储与受保护同步、离线筛选服务、CLI 和端到端验收均已落地，并已完成真实 Baostock 的单股票全接口联网冒烟。外部数据只能由 `DataSyncService` 串行获取；同一数据集和交易日成功后不会重复拉取，失败必须显式冷却重试。`screen` 和 `status` 只读既有 SQLite，`sync` 是唯一允许进入 Provider 同步路径的 CLI 子命令。

```bash
stock-manager screen --db data/market.sqlite3 --rules config/rules.json \
  --date 2026-08-25 --adjustment qfq --format summary

stock-manager status --db data/market.sqlite3 --lock-dir data/locks \
  --adjustment qfq
```

## Baostock 联网冒烟测试

先完成上面的可编辑安装。以下命令只通过 `DataSyncService` 访问 Baostock，并使用自动清理的临时 SQLite 与锁目录，不会写入正式市场数据库。它验证交易日历、股票全集、前复权日线、基本面和分红接口；需要联网，且不属于默认离线测试套件。

```bash
stock-manager smoke --config config/sync.json \
  --code sh.600000 --date 2025-09-19 --adjustment qfq
```

成功时会输出 JSON 格式的 Provider 冒烟结果。日期、股票代码和复权方式均为显式测试输入；更换日期前应先确认该日期存在于 Baostock 的历史行情覆盖范围。

## P3 本地 Web 工作台

P3 提供本地、离线优先的 Web 操作界面。先完成上面的可编辑安装，然后启动服务（默认只监听 127.0.0.1）：

```bash
stock-manager web \
  --db data/market.sqlite3 \
  --system-templates config/rule_templates \
  --user-templates data/user-templates \
  --static src/stock_manager/web/static \
  --host 127.0.0.1 \
  --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。页面按「运行条件 / 策略编辑器 / 筛选结果」三栏组织，规则参数控件由 `GET /api/rules` 元数据自动生成。接口与错误映射见 `docs/P3_WEB_API.md`，规则扩展见 `docs/P3_RULE_EXTENSION.md`。

如需在本地重建数据以观察结果，请先通过 `stock-manager sync` 拉取指定交易日数据；筛选始终只读取本地 SQLite。
