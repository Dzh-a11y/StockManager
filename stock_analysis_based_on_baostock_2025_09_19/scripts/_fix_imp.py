from pathlib import Path
p = Path("src/stock_manager/services/historical_screening_executor.py")
text = p.read_text(encoding="utf-8")
old = "from stock_manager.rules.builtin import build_default_registry  # noqa: E402
"
assert old in text
text = text.replace(old, "", 1)
old2 = "from stock_manager.rules.base import RuleContext"
new2 = "from stock_manager.rules.base import RuleContext
from stock_manager.rules.builtin import build_default_registry"
assert old2 in text
text = text.replace(old2, new2, 1)
p.write_text(text, encoding="utf-8")
print("import fixed")
