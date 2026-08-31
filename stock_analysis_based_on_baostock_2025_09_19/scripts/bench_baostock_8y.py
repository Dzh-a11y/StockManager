"""Benchmark: how long Baostock takes to pull 8 years of daily bars.

Usage:
    python scripts/bench_baostock_8y.py [--codes sh.600000,sz.000001] [--max-codes N]

Pulls daily bars (unadjusted) over START..END for a sample of A-share codes
using the project's BaostockProvider, prints per-request timing, and
extrapolates to the full A-share universe.

This is a network benchmark script; it is NOT part of the product code.
"""

from __future__ import annotations

import argparse
import time
from datetime import date
from typing import Sequence

from stock_manager.domain import AdjustmentMethod
from stock_manager.providers.baostock_provider import BaostockProvider

START = date(2018, 9, 1)
END = date(2026, 8, 31)
FIELDS = "date,code,open,high,low,close,preclose,volume,amount,tradestatus"

# A representative spread of large/mid-cap A-shares across exchanges.
_DEFAULT_CODES = [
    "sh.600000",  # 浦发银行
    "sz.000001",  # 平安银行
    "sh.600519",  # 贵州茅台
    "sz.000858",  # 五粮液
    "sh.601318",  # 中国平安
    "sz.300750",  # 宁德时代
    "sh.688981",  # 中芯国际
    "sz.002594",  # 比亚迪
    "sh.603259",  # 药明康德
    "sz.300059",  # 东方财富
]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", help="comma-separated baostock codes")
    parser.add_argument("--max-codes", type=int, default=0, help="cap the sample size (0 = all listed)")
    parser.add_argument("--interval", type=float, default=0.2, help="request interval seconds (provider default 0.2)")
    args = parser.parse_args(argv)

    provider = BaostockProvider(request_interval_seconds=args.interval)
    universe_size = 0

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",")]
    else:
        # Sample real A-share codes from the live universe listing.
        with provider._session():
            rows = provider._rows(
                provider._query(
                    lambda: provider._client.query_all_stock(day=END.isoformat())
                ),
                "query_all_stock",
            )
        codes = [
            row["code"]
            for row in rows
            if row["code"].split(".", maxsplit=1)[0].lower() in {"sh", "sz"}
            and row["code"].split(".", maxsplit=1)[1].startswith(
                ("600", "601", "603", "605", "688", "000", "001", "002", "003", "300", "301")
            )
        ]
        universe_size = len(codes)
        if args.max_codes:
            # Deterministic spread sample: stride through the list.
            stride = max(1, len(codes) // args.max_codes)
            codes = codes[::stride][: args.max_codes]
        print(f"universe listing: {universe_size} A-share codes fetched via query_all_stock")

    # --- warm-up: login timing -------------------------------------------------
    t0 = time.monotonic()
    with provider._session():
        login_s = time.monotonic() - t0
    print(f"login+logout round trip: {login_s:.2f}s")

    # --- timing window ---------------------------------------------------------
    per_code: list[float] = []
    total_rows = 0
    t_start = time.monotonic()
    with provider._session():
        for index, code in enumerate(codes):
            t0 = time.monotonic()
            result = provider._query(
                lambda code=code: provider._client.query_history_k_data_plus(
                    code,
                    FIELDS,
                    start_date=START.isoformat(),
                    end_date=END.isoformat(),
                    frequency="d",
                    adjustflag=provider._adjustflag(AdjustmentMethod.UNADJUSTED),
                )
            )
            rows = provider._rows(result, f"query_history_k_data_plus({code})")
            elapsed = time.monotonic() - t0
            per_code.append(elapsed)
            total_rows += len(rows)
            print(
                f"[{index + 1:>3}/{len(codes)}] {code}: {elapsed:6.2f}s, {len(rows):>4} rows"
            )
    t_end = time.monotonic()
    wall = t_end - t_start

    # --- summary ---------------------------------------------------------------
    avg = sum(per_code) / len(per_code)
    median = sorted(per_code)[len(per_code) // 2]
    print(f"\n=== sample of {len(codes)} codes, {START}..{END} ({wall:.1f}s wall) ===")
    print(f"rows pulled: {total_rows} ({total_rows / len(codes):.0f} rows/code)")
    print(f"per-code: avg {avg:.2f}s, median {median:.2f}s, min {min(per_code):.2f}s, max {max(per_code):.2f}s")

    universe = universe_size or args.max_codes or 0
    if universe:
        est = avg * universe
        print(f"universe={universe}: est. wall {est / 60:.1f} min (pure query time; no login)")
    else:
        print(f"universe=ALL A-shares (~5100): est. wall {avg * 5100 / 60:.1f} min")
        print(f"  at 0.2s/req pacing, minimum pacing floor alone: {0.2 * 5100 / 60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
