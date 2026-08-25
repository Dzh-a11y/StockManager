---
date: 2026-08-25
purpose: 说明 StockManager 使用的数据库以及本地 Web UI 的启动方式。
project: StockManager
status: active
---

# StockManager

StockManager 是面向 A 股的研究型筛选平台。所有筛选结果仅供研究参考，不构成任何投资建议；项目严禁实现自动交易功能。

## 数据库

本地使用 **SQLite**，路径为 `data/market.sqlite3`。市场数据由 `stock-manager sync` 通过 `DataSyncService` 拉取后写入；筛选规则、Web API 与 CLI 查询只读取本地 SQLite，不直接访问外部 Provider。

## Web UI 启动

先进入代码仓库并安装依赖：

```bash
cd /Users/douzihao/StockManager/stock_analysis_based_on_baostock_2025_09_19
python3 -m pip install '.[dev]'
```

启动本地服务（默认只监听 127.0.0.1）：

```bash
stock-manager web \
  --db data/market.sqlite3 \
  --system-templates config/rule_templates \
  --user-templates data/user-templates \
  --static src/stock_manager/web/static \
  --sync-config config/sync.json \
  --lock-dir data/locks \
  --host 127.0.0.1 \
  --port 8000
```

浏览器打开 `http://127.0.0.1:8000` 进入筛选工作台。
