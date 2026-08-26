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

### 一键启动（推荐）

**桌面图标(双击即用)**:
- macOS:把 `scripts/StockManager.command` 放到桌面或程序坞,双击即启动并自动打开浏览器
- Windows:把 `scripts/StockManager.bat` 放到桌面,双击即启动并自动打开浏览器
- 服务已在运行时再点会直接打开浏览器(幂等,不会重复启动)

**命令行方式**:
```bash
python3 scripts/launcher.py start    # 启动并打开浏览器
python3 scripts/launcher.py status   # 查看状态
python3 scripts/launcher.py restart  # 重启
python3 scripts/launcher.py stop     # 停止
```

启动器会自动:检测/创建 `.venv`、安装依赖、检查端口(已在跑则跳过)、写日志到 `data/server.log`、等待就绪后打开浏览器。

### 手动启动(等价,适合排障)

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
