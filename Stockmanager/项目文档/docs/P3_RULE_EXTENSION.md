---
date: 2026-08-25
purpose: 说明在 StockManager P3 中如何新增普通规则，并沿元数据链路自动出现在页面。
project: StockManager
status: active
---

# P3 规则扩展指南

普通规则扩展不需要修改筛选主循环、HTTP 路由或前端规则清单。新增规则只需：定义参数、声明数据需求、实现求值，并在 `build_default_registry` 显式注册。

## 标准路径

```text
新增纯规则与强类型参数
  -> 声明 RuleDefinition
  -> 声明 RuleDataRequirement
  -> build_default_registry 显式注册
  -> /api/rules 自动输出
  -> 前端自动生成参数控件
  -> 模板编译
  -> ParameterizedScreeningService 执行
  -> 结果页展示 RuleExecutionResult
```

## 具体步骤

### 1. 定义纯函数与强类型参数

在 `src/stock_manager/rules/` 下新增规则模块，返回结构化的 `RuleResult`：

```python
from stock_manager.domain import RuleResult


def evaluate_example(context, threshold):
    actual = len(context.daily_bars)
    return RuleResult(
        rule_id="example",
        passed=actual >= threshold,
        actual_value=actual,
        threshold=threshold,
        reason=f"bars {actual} vs threshold {threshold}",
    )
```

### 2. 通过 `RuleDefinition` 暴露元数据

在 `src/stock_manager/rules/builtin.py` 中新增 adapter，定义 `definition`、`parse_parameters`、`data_requirement`、`evaluate`：

```python
from stock_manager.rules.base import (
    ParameterDefinition,
    ParameterType,
    RuleDataRequirement,
    RuleDefinition,
)


class ExampleRule:
    definition = RuleDefinition(
        "example",
        "示例规则",
        "一段说明",
        (ParameterDefinition("threshold", ParameterType.INTEGER, True, 1, None, None, "阈值", "整数阈值"),),
    )

    def parse_parameters(self, raw):
        # 强类型校验
        ...

    def data_requirement(self, parameters):
        return RuleDataRequirement(market_history_unit=..., history_length=...)

    def evaluate(self, context, parameters):
        return evaluate_example(context, parameters)
```

### 3. 显式注册

在 `src/stock_manager/rules/builtin.py` 的 `build_default_registry()` 中加入 `ExampleRule()`。

注册后：

- `GET /api/rules` 自动包含新规则及全部参数。
- 前端通用渲染器根据 `value_type`（整数、小数、布尔、文本）自动生成控件。
- 模板可启用该规则、编译并返回逐规则 `PASSED`/`FAILED`/`SKIPPED` 结果。

## 需要框架级升级的情况

仅当以下情况需要编写 ADR 并扩展领域与 Repository 边界，禁止在规则内部绕过分层：

- 新增参数类型或组合运算符。
- 规则需要 `RuleContext` 尚不支持的新数据域。

## 验收红线

- 规则与 Domain 不依赖 HTTP、HTML、JavaScript 或 Provider。
- 规则不得自行查库或联网；数据需求通过 `RuleDataRequirement` 声明。
- 普通新规则不得要求修改筛选主循环、HTTP 路由或前端规则清单。
