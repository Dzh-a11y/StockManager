---
date: 2026-09-02
purpose: 说明 StockManager 的功能、内置筛选规则、回测策略以及 macOS / Windows 下本地 Web 工作台的最新启动方式。
project: StockManager
status: active
---

# StockManager

StockManager 是面向 A 股的研究型筛选平台。所有筛选结果仅供研究参考，不构成任何投资建议；项目严禁实现自动交易功能。

## 版本与分支

当前主分支版本：**1.13.9**。

2026-09-02 将 `codex/fix-datasync-stability` 的 13 个开发提交及需求澄清、任务职责文档更新合入本地 `main`。1.13.8 的回测 Worker 数配置和 1.13.9 的回测前置提示均包含在内；原主分支版本为 1.12.0。

包版本以所用分支的 `pyproject.toml`、`src/stock_manager/__init__.py` 和 `tests/test_package_structure.py` 为准，三处均为 1.13.9。本次合并不额外升版；本地合并不代表已推送远端或发布新版本。

## 需求沟通与开发协作

新需求默认采用“你提需求，Agent 大量、系统地追问细化”的方式，不设每轮问题数量上限。问题按主题分组并编号，可分批讨论并持续追问，充分明确场景、范围、接口参数、业务与数据口径、交互异常、任务依赖及验收标准，形成尽量精确、可执行、可验收的项目计划（plan），确认后实施。每轮问答持续更新计划和待决清单，避免实现者猜测关键需求。已经确认并授权的工作及范围明确的小修改直接执行，避免重复确认。

模型职责由每次提示词指定，例如“你负责架构”“你负责实现”或“你负责审查”；项目不固定模型分工。实现任务包含必要测试、自审和文档同步，架构讨论按约定范围交付方案。

完整约定见 [项目开发规定](AGENTS.md) 第 7 节。

## 功能概览

- **本地优先**：行情数据由同步服务从 Baostock 拉取后写入本地 SQLite（`data/market.sqlite3`）；筛选、Web 与 CLI 查询只读本地数据库，断网可运行。
- **参数化筛选**：内置 12 条可配置规则，模板可开关规则、调整参数与组合；可配置筛选并发数（Worker 数 1~16，默认 4），worker 越多越快但内存与 CPU 占用越高（实测 8~16 为收益峰值，超过 16 无增益）；筛选完成后显示本次用时。
- **本地 Web 工作台**：加载/编辑模板、开关规则、修改参数、调整组合、运行筛选并查看逐股结果与进度；点击筛选出的个股自动展示本地日K线与成交量，K线信息栏同时展示「涨幅次数」规则命中的涨幅日期。
- **数据状态可视化**：顶部显示包版本号；“数据状态”卡片按自然日渲染近 30 天逐日同步状态色块（已同步=绿、同步中/未完全同步=琥珀、失败=红、缺失=灰、非交易日=浅），并用 11 段约 30 天窗口展示近 360 天覆盖率；覆盖不足（未完全同步）的天以琥珀色提示。
- **自动补齐**：进入新的已完成交易日后首次启动自动同步补齐历史数据；同一交易日同步成功后不会重复拉取，标记为失败或不完整的交易日会在后续回补时重新拉取刷新。
- **双平台一键启动**：macOS 与 Windows 均支持双击启动，首次运行自动创建虚拟环境并安装依赖。
- **可替换只读数据层（P4）**：筛选/回测/研究统一通过数据库无关的只读访问层读取本地数据（分片、并发、稳定排序、快照一致性），未来可无缝替换 SQLite 为 DuckDB/PostgreSQL 等后端。
- **策略编辑器与回测（P5A）**：Web 工作台内置「回测策略」编辑器，从六类政策（入场/退出/再平衡/分配/排名/执行）中各选其一并填写参数（参数白名单校验，禁止任意代码）；回测窗口以年为单位（1~8 年），终点为本地数据最新已完成交易日，起点按 N×260 个交易日向前；回测由 Backtrader 引擎驱动，采用 A 股执行模型（T+1 卖出约束、次日开盘成交、整手、停牌/涨跌停限制、佣金/印花税/过户费）。
- **策略机制**：内置回调入场（筛选通过后等价格自近 N 日高点回落再买入）、止盈减仓（涨幅达到阈值后卖出部分持仓）、补仓（买入后成本回撤且有现金则加仓）、均线策略（低于 N 日均线买入、高于 N 日均线卖出）等机制，可与资格、排名、执行政策自由组合。
- **八年历史回补**：首次启动自动以「最新已完成交易日」为终点向前回补八年历史行情（统一前复权），断点续传、瞬时网络失败按冷却自动重试，批次内逐代码实时进度展示。

## 目录结构

- 项目根目录：`/Users/douzihao/StockManager`
- 代码仓库：`/Users/douzihao/StockManager/stock_analysis_based_on_baostock_2025_09_19`
- 项目文档（project_doc）：`/Users/douzihao/StockManager/project_doc`

## 数据库

本地使用 **SQLite**，路径为 `data/market.sqlite3`（相对于代码仓库）。市场数据由 `stock-manager sync` 通过 `DataSyncService` 拉取后写入；筛选规则、Web API 与 CLI 查询只读取本地 SQLite，不直接访问外部 Provider。交易日以 `Asia/Shanghai` 为准，同步目标为“最新已完成交易日”。

## 启动方式

先进入代码仓库：

```bash
cd /Users/douzihao/StockManager/stock_analysis_based_on_baostock_2025_09_19
```

首次运行会自动检测 Python 3.11+、创建 `.venv`、执行 `pip install -e '.[dev]'`（下载 baostock/tzdata 等，约 1–2 分钟），然后启动服务并打开浏览器；之后再次启动秒进，不再安装。服务已在运行时重复点击只会打开浏览器（幂等，不会重复启动）。

### macOS

- **双击脚本（推荐，最简）**：把 `scripts/StockManager.command` 放到桌面或程序坞，双击即启动并自动打开浏览器。
  - 注意：该脚本第一行写死了本项目路径；如果整个项目目录移动了，把那一行的路径改成新路径即可。
- **桌面 App（带图标）**：先运行 `python3 scripts/make_icon.py` 生成图标（若已生成可跳过），再运行 `./scripts/make_app.sh`，在代码仓库根生成 `StockManager.app`，双击图标即可启动。
- **命令行方式**：

  ```bash
  python3 scripts/launcher.py start    # 启动并打开浏览器
  python3 scripts/launcher.py status   # 查看运行状态
  python3 scripts/launcher.py restart  # 重启
  python3 scripts/launcher.py stop     # 停止
  ```

### Windows

- **双击启动（推荐，最傻瓜）**：把项目拷到 Windows 后，直接双击 `scripts\StockManager.bat`。首次会自动查找 Python 3.11+、创建 `.venv`、安装依赖、启动服务、打开浏览器，并**自动在桌面生成带图标的 StockManager 快捷方式**；之后双击桌面图标即可。
- **快捷方式补救**：若桌面图标缺失，双击 `scripts\setup_windows.bat` 重新生成（快捷方式指向 `StockManager.bat`）。
- **单文件 exe（可选，免装 Python）**：在 Windows 上双击 `scripts\build_windows_exe.bat` 打包，产物为 `dist\StockManager.exe`；把它复制到任意文件夹双击即可运行，数据存放在 exe 所在目录。
- **命令行方式**（在代码仓库根执行）：

  ```bat
  scripts\launcher.py start    & 启动并打开浏览器（首次自动装依赖）
  ```

  也可直接双击 `scripts\StockManager.bat` 走同一流程。

### 手动启动（等价，适合排障）

不依赖启动器时，用已安装的 CLI 等价启动：

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

浏览器打开 `http://127.0.0.1:8000` 进入筛选工作台。日志写入 `data/server.log`，进程信息在 `data/server.pid`。

## 内置筛选规则

规则通过模板配置开关与参数，Web 工作台的规则目录由后端元数据自动生成（`GET /api/rules`）：

| rule_id | 名称 | 说明 |
| --- | --- | --- |
| `pe_positive` | PE 下限 | PE TTM 必须存在并严格大于下限；用于排除亏损或微利股票 |
| `non_st` | 排除 ST | 股票不得标记为 ST |
| `volume_price_5d` | 量价信号 | 相邻交易日同时满足量比和收盘涨幅 |
| `limit_up_breakout` | 炸板或假阴线 | 检查涨停炸板或假阴线信号 |
| `limit_up_3m` | 涨幅次数 | 统计窗口内涨幅事件次数 |
| `volatility_multiple` | 波动倍数 | 限制窗口最高价与最低价的倍数 |
| `annual_min_volume` | 年度最低交易量 | 目标日是否为自然日窗口最低交易量 |
| `annual_min_close_price` | 年度最低收盘价 | 目标日是否为自然日窗口最低收盘价 |
| `consecutive_up_days` | 连阳 | 最近N个交易日窗口内出现至少K个连续上涨交易日（K连阳） |
| `n_day_close_above` | N日收盘价下限 | 最近N个交易日每天的收盘价都严格高于设定值 |
| `volume_sum_extreme` | 连续量能极值 | 最近连续N天的成交量之和是所有连续M天成交量之和中的最低值或最高值 |
| `price_range_ratio` | N日高低点倍率 | 最近N个交易日内最高价相对最低价的倍数落在指定区间内 |

系统默认模板把基本面（PE、非 ST）设为全部满足、信号组（量价、炸板、年度最低量/最低价）任一满足、风险组（涨幅次数、波动）全部满足。


## 回测与研究（P5A）

历史筛选 + Backtrader 一体化回测。工作台的「回测策略」编辑器按六类政策各选其一（策略目录由 `GET /api/research/policies` 自动生成）：

| 类别 | policy_id | 说明 |
| --- | --- | --- |
| 入场 entry | `eligibility_enter_v1` | 资格名单出现即允许进入（服从执行约束） |
| 入场 entry | `pullback_entry_v1` | 筛选通过后不立即买入，等价格自近 N 日高点回落 drawdown_ratio 后再买入（回调入场） |
| 入场 entry | `sma_below_v1` | 收盘价低于 N 日均线时买入（均线买入，均值回归） |
| 退出 exit | `eligibility_exit_v1` | 不再合格即退出 |
| 退出 exit | `sma_timing_v1` | 收盘价跌破 N 日均线时退出 |
| 退出 exit | `fixed_holding_v1` | 持有 N 个交易日后退出 |
| 退出 exit | `take_profit_partial_v1` | 上涨到 take_profit_ratio 后卖出一定比例（止盈减仓） |
| 退出 exit | `sma_above_v1` | 收盘价高于 N 日均线时卖出（均线卖出，均值回归） |
| 再平衡 rebalance | `daily_v1` | 每个交易日收盘后重算目标组合 |
| 分配 allocation | `equal_weight_v1` | 等权分配：每只目标股资金 = 净资产 / 目标持仓数 |
| 分配 allocation | `add_position_on_dip_v1` | 持有中回调 add_drawdown_ratio 且有现金则按 add_fraction 补仓（最多 max_additions 次） |
| 排名 ranking | `turnover_20d_desc_v1` | 按近 20 日平均换手率降序取候选 |
| 执行 execution | `ashare_execution_v1` | A 股执行模型：次日开盘成交、T+1、整手、停牌/涨跌停、佣金/印花税/过户费 |

回测窗口以年为单位（1~8 年）：终点为本地数据最新已完成交易日，起点按 N×260 个交易日向前；回测结果包含净值曲线、收益/回撤/夏普指标与逐笔成交记录。回测依赖八年历史覆盖，首次启动自动回补（断点续传，批次内逐代码实时进度）。

## 文档

项目文档与架构决策记录（ADR）位于 `/Users/douzihao/StockManager/project_doc`，按「开发手册」与「使用手册」组织（目录名均为英文）：

- `development/plan/`：计划手册（`PHASE_*_PLAN.md`、`MIGRATION_PLAN.md`、`DELETE_MANIFEST.md`）。
- `development/architecture/`：架构手册与架构图（`ADR_OVERVIEW.md` 为汇总总体架构决策的总 ADR，含 `ADR_P*` 明细与 HTML/JSON 架构图）。
- `development/implementation/`：实现手册（各阶段设计、验收与接口明细，如 `P1_*`、`P2_*`、`P3_WEB_API.md`）。
- `usage/README.md`：使用手册（安装、启动、内置规则与操作说明）。

入口索引见 `project_doc/README.md`。
