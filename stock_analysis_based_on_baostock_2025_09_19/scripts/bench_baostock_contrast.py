"""Controlled comparison: per-request timing without query_all_stock first.

Measures 8 consecutive history requests for fixed codes right after a fresh
login, and prints the SDK error_code so retries/rate-limits are visible.
"""

from __future__ import annotations

import socket
import time

import baostock as bs

START = "2018-09-01"
END = "2026-08-31"
FIELDS = "date,code,open,high,low,close,preclose,volume,amount,tradestatus"
CODES = ["sh.600000", "sz.000001", "sh.600519", "sz.000858", "sh.601318",
         "sh.600000", "sz.000001", "sh.600519"]

result = bs.login()
print(f"login: error_code={result.error_code} error_msg={result.error_msg}")
last: float | None = None
for i, code in enumerate(CODES):
    now = time.monotonic()
    if last is not None:
        gap = now - last
        if gap < 0.2:
            time.sleep(0.2 - gap)
    t0 = time.monotonic()
    rs = bs.query_history_k_data_plus(
        code, FIELDS, start_date=START, end_date=END, frequency="d", adjustflag="3"
    )
    elapsed = time.monotonic() - t0
    last = time.monotonic()
    rows = 0
    while rs.error_code == "0" and rs.next():
        rs.get_row_data()
        rows += 1
    print(f"[{i + 1}] {code}: {elapsed:.2f}s, error_code={rs.error_code}, rows={rows}")
bs.logout()
