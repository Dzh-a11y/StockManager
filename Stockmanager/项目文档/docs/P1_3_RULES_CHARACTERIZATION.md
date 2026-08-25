---
date: 2026-08-25
purpose: 说明 P1-3 旧筛选规则特征测试的范围、数据和验收结果。
project: StockManager
status: active
---

# P1-3 规则特征测试

## 范围

P1-3 为当前实际筛选路径建立行为护栏，不改动旧业务代码。覆盖 PE TTM、ST、五日四倍量且涨幅 7%、五日炸板与最高价、五日假阴线、三个月涨停以及波动倍数规则；同时覆盖空数据、`None`、乱序、零成交量、精确 -10% 和缺失值等边界数据。

需要联网的旧分红检查不进入单元测试，避免测试访问 Baostock。未出现在当前筛选组合中的旧规则暂不修改，也不宣称已经完成迁移。

## 固定数据

`tests/fixtures/legacy_rules_market_data.json` 是离线可复现数据，所有数据集均显式记录：

- 来源：`legacy fixture`
- 生成时间：`2026-08-25T12:00:00+08:00`
- 时区：`Asia/Shanghai`
- 复权：`qfq`
- 历史依据：旧入口 `adjustflag=2`

每条非空行情固定为 `date/open/high/low/close/volume` 六个字段。日期相关测试冻结旧模块时间，避免随运行日期漂移。

## 行为护栏

P1-3 完成时，`tests/test_rules_characterization.py` 曾直接执行旧实现并固定其可疑行为，详细条目见根目录 `AMBIGUITY_LOG.md`。P1-4 至 P1-7 完成、现行纯规则测试通过后，该测试与旧源码已于 2026-08-25 一并移出代码仓库，保存在项目归档 `legacy_stockmanager_2025_archive`。固定 fixture 继续留在 `tests/fixtures/legacy_rules_market_data.json`，作为历史行为证据；现行语义由 `tests/test_rules.py` 验证。

## 验收结果

在 Python 3.14 项目虚拟环境中执行：

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
```

结果：`30 passed`。测试未联网，未调用外部行情 Provider。
