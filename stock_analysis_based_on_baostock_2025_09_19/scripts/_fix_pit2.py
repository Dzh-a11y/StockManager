from pathlib import Path
p = Path("src/stock_manager/read/historical.py")
text = p.read_text(encoding="utf-8")

# 找到实现类里的 trading_days(第二次出现)
first = text.index("    def trading_days(self, start: date, end: date) -> tuple[date, ...]:")
second = text.index("    def trading_days(self, start: date, end: date) -> tuple[date, ...]:", first + 1)
anchor = text[second:second + 200]
assert "self._assert_open()" in anchor

addition = """    def all_universe_snapshots(
        self,
    ) -> tuple[tuple[date, tuple[StockIdentity, ...]], ...]:
        """All stock snapshots grouped by as_of, ordered by as_of.

        Workers load the whole snapshot series once and derive universe_as_of
        in memory for every evaluation day (no per-day database queries).
        """
        self._assert_open()
        with self._connection:
            rows = self._connection.execute(
                """SELECT * FROM stocks ORDER BY as_of, code"""
            ).fetchall()
        grouped: dict[date, list[StockIdentity]] = {}
        for row in rows:
            as_of = date.fromisoformat(row["as_of"])
            grouped.setdefault(as_of, []).append(
                StockIdentity(
                    code=row["code"],
                    name=row["name"],
                    exchange=row["exchange"],
                    is_st=bool(row["is_st"]),
                    listed_on=(
                        None if row["listed_on"] is None
                        else date.fromisoformat(row["listed_on"])
                    ),
                    delisted_on=(
                        None if row["delisted_on"] is None
                        else date.fromisoformat(row["delisted_on"])
                    ),
                )
            )
        return tuple(
            (as_of, tuple(items)) for as_of, items in sorted(grouped.items())
        )

"""
# 插入到实现类 trading_days 方法之前
text = text[:second] + addition + text[second:]
p.write_text(text, encoding="utf-8")
print("implementation method added")
