"""Data-quality checks that must pass before interpreting a backtest."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True)
class DataAudit:
    integrity: str
    active_securities: int
    symbols_with_bars: int
    rows: int
    first_day: str | None
    latest_day: str | None
    latest_day_symbols: int
    newest_observed_day: str | None
    newest_observed_symbols: int
    no_data_symbols: int
    lagging_symbols: int
    failures: int

    @property
    def ok(self) -> bool:
        return self.integrity == "ok" and self.no_data_symbols == 0 and self.lagging_symbols == 0 and self.failures == 0


def audit_database(conn: sqlite3.Connection, max_lag_days: int = 7, coverage_threshold: float = 0.9) -> DataAudit:
    """Compare each active symbol with the latest date present in the database.

    Calendar-day lag is intentional: it is a conservative warning and does not
    attempt to guess exchange holidays.  A warning requires review, not a blind
    deletion or automatic data repair.
    """
    if max_lag_days < 0:
        raise ValueError("max_lag_days 不能为负数")
    if not 0 < coverage_threshold <= 1:
        raise ValueError("coverage_threshold 必须在 (0, 1] 区间")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM securities WHERE is_active = 1").fetchone()[0]
    rows, first_day, newest_day = conn.execute("SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM daily_kline").fetchone()
    if not newest_day:
        return DataAudit(integrity, active, 0, rows, first_day, None, 0, None, 0, active, 0, conn.execute("SELECT COUNT(*) FROM download_failures").fetchone()[0])
    with_bars = conn.execute("SELECT COUNT(DISTINCT symbol) FROM daily_kline").fetchone()[0]
    newest_symbols = conn.execute("SELECT COUNT(*) FROM daily_kline WHERE trade_date = ?", (newest_day,)).fetchone()[0]
    complete = conn.execute(
        """SELECT trade_date, COUNT(*) AS symbol_count FROM daily_kline
           GROUP BY trade_date HAVING symbol_count >= ? ORDER BY trade_date DESC LIMIT 1""",
        (active * coverage_threshold,),
    ).fetchone()
    latest_day, latest_symbols = complete if complete else (None, 0)
    no_data = conn.execute(
        "SELECT COUNT(*) FROM securities s WHERE s.is_active = 1 AND NOT EXISTS (SELECT 1 FROM daily_kline d WHERE d.symbol = s.symbol)"
    ).fetchone()[0]
    lagging = 0 if not latest_day else conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT s.symbol, MAX(d.trade_date) AS last_day
               FROM securities s JOIN daily_kline d ON d.symbol = s.symbol
               WHERE s.is_active = 1 GROUP BY s.symbol
               HAVING julianday(?) - julianday(last_day) > ?
           )""", (latest_day, max_lag_days)
    ).fetchone()[0]
    failures = conn.execute("SELECT COUNT(*) FROM download_failures").fetchone()[0]
    return DataAudit(integrity, active, with_bars, rows, first_day, latest_day, latest_symbols, newest_day, newest_symbols, no_data, lagging, failures)
