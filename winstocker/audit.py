"""Data-quality checks that must pass before interpreting a backtest."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from .calendar import calendar_schema_present, currently_suspended

# 相邻两行若出自同一次抓取，定点整数必然满足 change_amount == close - prev_close
# （build_kline_rows 正是这么算的）。跨越前复权尺度边界时该恒等式必然破缺，
# 因此它是「静默尺度漂移」的精确检测器——只看 MAX(trade_date) 的覆盖率检查
# 在结构上不可能发现这类损坏。正常库应恒为 0。
BROKEN_CHANGE_SQL = """
WITH s AS (
    SELECT symbol_id, trade_date, close, change_amount,
           LAG(close) OVER (PARTITION BY symbol_id ORDER BY trade_date) AS prev_close
    FROM kline
)
SELECT sec.symbol, s.trade_date FROM s
JOIN securities sec ON sec.id = s.symbol_id
WHERE s.prev_close IS NOT NULL AND s.change_amount IS NOT NULL
  AND s.change_amount != s.close - s.prev_close
"""


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
    # 无日线的股票大多只是尚未上市（接口返回 0 根），不是抓取失败。
    no_data_failed: int = 0
    # 落后的股票若正处停牌，则是行情的正常形态而非数据缺陷。
    lagging_failed: int = 0
    suspended_symbols: int = 0
    broken_change_rows: int = 0

    @property
    def ok(self) -> bool:
        """只有「真实缺陷」才是否决项：未上市与停牌都是正常形态。"""
        return (
            self.integrity == "ok"
            and self.no_data_failed == 0
            and self.lagging_failed == 0
            and self.broken_change_rows == 0
            and self.failures == 0
        )


def scale_drift(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """返回 (股票代码, 交易日)：该处前复权序列跨越了尺度边界。

    相邻两行若出自同一次抓取，change_amount 必然等于 close 减前收盘价；一旦第 d-1 行
    停留在旧尺度上（除权后只重写了最新几天），恒等式就在第 d 行破缺。这不需要联网，
    也不依赖覆盖率查询，因此既能做审计哨兵，也能直接驱动修复。
    """
    try:
        return [(row[0], row[1]) for row in conn.execute(BROKEN_CHANGE_SQL)]
    except sqlite3.OperationalError:
        return []


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
    failures = conn.execute("SELECT COUNT(*) FROM download_failures").fetchone()[0]
    broken = len(scale_drift(conn))
    if not newest_day:
        return DataAudit(integrity, active, 0, rows, first_day, None, 0, None, 0, active, 0, failures,
                         no_data_failed=0, lagging_failed=0, suspended_symbols=0, broken_change_rows=broken)
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
    # 不在 download_failures 里 = 最近一次抓取成功 = 接口本身就没有数据（多为未上市）。
    no_data_failed = conn.execute(
        """SELECT COUNT(*) FROM securities s
           WHERE s.is_active = 1
             AND NOT EXISTS (SELECT 1 FROM daily_kline d WHERE d.symbol = s.symbol)
             AND EXISTS (SELECT 1 FROM download_failures f WHERE f.symbol = s.symbol)"""
    ).fetchone()[0]
    lagging_symbols: set[str] = set()
    if latest_day:
        lagging_symbols = {row[0] for row in conn.execute(
            """SELECT s.symbol FROM securities s JOIN daily_kline d ON d.symbol = s.symbol
               WHERE s.is_active = 1 GROUP BY s.symbol
               HAVING julianday(?) - julianday(MAX(d.trade_date)) > ?""",
            (latest_day, max_lag_days),
        )}
    # 停牌才是这些股票「落后」的原因时，不算数据缺陷。
    suspended = currently_suspended(conn, latest_day) if calendar_schema_present(conn) else set()
    suspended_all = 0
    if calendar_schema_present(conn):
        suspended_all = conn.execute(
            """SELECT COUNT(DISTINCT x.symbol_id) FROM suspensions x
               JOIN securities s ON s.id = x.symbol_id WHERE s.is_active = 1"""
        ).fetchone()[0]
    lagging_failed = len(lagging_symbols - suspended)
    return DataAudit(integrity, active, with_bars, rows, first_day, latest_day, latest_symbols,
                     newest_day, newest_symbols, no_data, len(lagging_symbols), failures,
                     no_data_failed=no_data_failed, lagging_failed=lagging_failed,
                     suspended_symbols=suspended_all, broken_change_rows=broken)
