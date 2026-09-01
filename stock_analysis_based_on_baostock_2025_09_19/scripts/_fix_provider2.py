from pathlib import Path
p = Path("src/stock_manager/providers/baostock_provider.py")
text = p.read_text(encoding="utf-8")

old = '''        fields = "date,code,open,high,low,close,preclose,volume,amount,tradestatus"
        with self._session():
            for index, code in enumerate(codes):
                self._emit_progress("daily_bars", index + 1, len(codes), code)
                rows = self._rows(
                    self._query(
                        lambda code=code: self._client.query_history_k_data_plus(''
new = '''        fields = "date,code,open,high,low,close,preclose,volume,amount,tradestatus"
        with self._session() as relogin:
            for index, code in enumerate(codes):
                self._emit_progress("daily_bars", index + 1, len(codes), code)
                rows = self._rows(
                    self._query(
                        lambda code=code: self._client.query_history_k_data_plus(''
assert old in text, "bars header not found"
text = text.replace(old, new, 1)

old2 = '''                            adjustflag=self._adjustflag(adjustment),
                        )
                    ),
                    f"query_history_k_data_plus({code})",
                )
                for row in rows:
                    bars.append(self._daily_bar(row))'''
new2 = '''                            adjustflag=self._adjustflag(adjustment),
                        ),
                        relogin=relogin,
                    ),
                    f"query_history_k_data_plus({code})",
                )
                for row in rows:
                    bars.append(self._daily_bar(row))'''
assert old2 in text, "bars query tail not found"
text = text.replace(old2, new2, 1)

old3 = '''        fields = "date,code,peTTM,pbMRQ"
        with self._session():
            for index, code in enumerate(codes):
                self._emit_progress("fundamentals", index + 1, len(codes), code)
                rows = self._rows(
                    self._query(''
new3 = '''        fields = "date,code,peTTM,pbMRQ"
        with self._session() as relogin:
            for index, code in enumerate(codes):
                self._emit_progress("fundamentals", index + 1, len(codes), code)
                rows = self._rows(
                    self._query(''
assert old3 in text, "fundamentals header not found"
text = text.replace(old3, new3, 1)

old4 = '''                            frequency="d",

                            adjustflag="3",
                        )
                    ),
                    f"query_history_k_data_plus({code})",
                )
                for row in rows:
                    snapshots.append(self._fundamental_snapshot(row, as_of))'''
new4 = '''                            frequency="d",

                            adjustflag="3",
                        ),
                        relogin=relogin,
                    ),
                    f"query_history_k_data_plus({code})",
                )
                for row in rows:
                    snapshots.append(self._fundamental_snapshot(row, as_of))'''
assert old4 in text, "fundamentals query tail not found"
text = text.replace(old4, new4, 1)

p.write_text(text, encoding="utf-8")
print("bars/fundamentals patched")
