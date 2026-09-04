"""Offline structural/accessibility contracts for the compact workbench."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


STATIC_ROOT = Path(__file__).resolve().parents[1] / "src/stock_manager/web/static"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class LayoutParser(HTMLParser):
    """Capture IDs and ancestor identities without any browser dependency."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, str | None]] = []
        self.nodes: dict[str, tuple[str, dict[str, str | None], tuple[str, ...]]] = {}
        self.toggles: list[tuple[dict[str, str | None], tuple[str, ...]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        ancestors = tuple(identity for _, identity in self.stack if identity)
        identity = attributes.get("id")
        if identity:
            assert identity not in self.nodes, f"duplicate DOM ID: {identity}"
            self.nodes[identity] = (tag, attributes, ancestors)
        if "data-panel-toggle" in attributes:
            assert tag == "button"
            self.toggles.append((attributes, ancestors))
        if tag not in VOID_TAGS:
            self.stack.append((tag, identity))

    def handle_endtag(self, tag: str) -> None:
        assert self.stack and self.stack[-1][0] == tag, f"unbalanced closing tag: {tag}"
        self.stack.pop()


def parse_layout() -> LayoutParser:
    parser = LayoutParser()
    parser.feed((STATIC_ROOT / "index.html").read_text(encoding="utf-8"))
    parser.close()
    assert not parser.stack
    return parser


def test_every_main_module_has_an_accessible_independent_collapse_control() -> None:
    layout = parse_layout()
    panels = {identity for identity, (_, attrs, _) in layout.nodes.items() if "data-panel-id" in attrs}
    assert panels == {
        # 工作台(P5):筛选运行、筛选模版、筛选结果、个股研究、维护
        "screen-panel", "template-panel", "results-panel",
        "stock-detail-panel", "maintenance-panel",
        # 回测系统(P5C):基础数据、策略模版、回测结果、历史运行
        "bt-basic-panel", "bt-strategy-panel", "bt-result-panel", "bt-history-panel",
    }
    assert len(layout.toggles) == len(panels)
    for attrs, ancestors in layout.toggles:
        panel = attrs["data-panel-toggle"]
        assert panel in panels and panel in ancestors
        assert attrs["type"] == "button"
        assert attrs["aria-expanded"] == "true"
        target = str(attrs["aria-controls"])
        assert panel in layout.nodes[target][2]


def test_backtest_view_has_own_navigation_and_unique_panel_ids() -> None:
    layout = parse_layout()
    for identity in (
        "backtest-view", "bt-back-workbench", "bt-open-data",
        "bt-run", "bt-cancel", "bt-codes", "bt-window-preset",
        "bt-strategy-select", "bt-entry-items", "bt-exit-items", "bt-tp-items",
        "bt-singles", "bt-result-content", "bt-history-list",
        "bt-replay-overlay", "bt-replay-canvas",
    ):
        assert identity in layout.nodes, f"missing backtest-view node: {identity}"
    assert layout.nodes["bt-back-workbench"][2][-1] == "backtest-view"


def test_stock_research_is_independent_and_ordered_before_maintenance() -> None:
    layout = parse_layout()
    order = list(layout.nodes)
    assert order.index("results-panel") < order.index("stock-detail-panel") < order.index("maintenance-panel")
    assert layout.nodes["stock-detail-panel"][2][-1] == "workbench-view"
    assert "result-body" not in layout.nodes["stock-detail-panel"][2]
    assert order.index("stock-rules") < order.index("stock-kline") < order.index("stock-capm-results")
    for identity in ("stock-rules", "kline-canvas", "stock-capm-results"):
        assert "stock-detail-analysis" in layout.nodes[identity][2]
    for identity in ("capm-benchmark", "capm-rate-term", "capm-periods-per-year"):
        assert "stock-detail-content" in layout.nodes[identity][2]
        assert "stock-detail-analysis" not in layout.nodes[identity][2]
    assert layout.nodes["capm-periods-per-year"][1]["value"] == "252"


def test_model_guide_distinguishes_daily_intercept_and_historical_evidence() -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    for expected in (
        "α日 = ȳ − β x̄", "α年化 = α日 × 年化因子", "α日 + εₜ",
        "并非逐日另算一条 α 序列", "[前收盘日, 当前收盘日)", "自然日数 / 365",
        "0.02%", "5.04%", "未计算标准误、p 值或置信区间", "不等同于未来",
        "同质预期", "行业指数并不是全市场组合", "Fama 与 French（2004）",
    ):
        assert expected in html
    css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
    assert ".stock-analysis-grid { grid-template-columns: 1fr; }" in css
    assert "[hidden] { display: none !important; }" in css
