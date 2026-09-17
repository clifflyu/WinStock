"""交易日历与停牌推导：把「日线缺口」变成一等公民。

停牌不是抓来的，是从 kline 的缺口推导出来的。推导成立的前提是每只股票最近一次
抓取成功（即不在 download_failures 中）——抓取成功而当天无行情，就只可能是停牌。
方向是安全的：被中断的 update 会把未处理股票留成「失败」，从而被标为待确认，
而不是被静默豁免成停牌。

日历本身也能顺手解决「今天还没收盘」：盘中那天只有少数股票有行情，覆盖率远低于
阈值，因此自动被排除在日历之外，不需要额外判断。
"""
from __future__ import annotations

import sqlite3

CALENDAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS trading_calendar (
    trade_date TEXT PRIMARY KEY
);
-- 只存缺口，全库约 4700 行（kline 表的 0.14%），物化后查询是常数级。
CREATE TABLE IF NOT EXISTS suspensions (
    symbol_id INTEGER NOT NULL,
    trade_date TEXT NOT NULL,
    PRIMARY KEY (symbol_id, trade_date)
) WITHOUT ROWID;
"""

# 与 audit_database 的 coverage_threshold 保持一致：某日有行情的股票数低于活跃
# 证券数的这个比例时，该日不算交易日（盘中半天就属于这种情况）。
COVERAGE_THRESHOLD = 0.9


def calendar_schema_present(conn: sqlite3.Connection) -> bool:
    """旧库或手搭的测试库可能没有这两张表；消费方据此降级而不是报错。"""
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return {"trading_calendar", "suspensions"} <= names


def refresh_calendar(conn: sqlite3.Connection, coverage_threshold: float = COVERAGE_THRESHOLD) -> tuple[int, int]:
    """整体重建日历与停牌表，返回 (交易日数, 停牌行数)。全量重建实测约 2.7 秒。"""
    if not 0 < coverage_threshold <= 1:
        raise ValueError("coverage_threshold 必须在 (0, 1] 区间")
    conn.executescript(CALENDAR_SCHEMA)
    active = conn.execute("SELECT COUNT(*) FROM securities WHERE is_active = 1").fetchone()[0]
    if not active:
        return 0, 0
    days = [row[0] for row in conn.execute(
        """SELECT trade_date FROM daily_kline GROUP BY trade_date
           HAVING COUNT(*) >= ? ORDER BY trade_date""",
        (active * coverage_threshold,),
    )]
    with conn:
        conn.execute("DELETE FROM trading_calendar")
        conn.execute("DELETE FROM suspensions")
        if not days:
            return 0, 0
        conn.executemany("INSERT INTO trading_calendar(trade_date) VALUES (?)", [(day,) for day in days])
        # 区间从该股首个交易日起、一直取到日历最后一天：这样「停在最后一天之前」
        # （即仍在停牌）也会被记为停牌，audit 才能把停牌导致的落后与真正的漏抓区分开。
        # 起点用该股自己的首个交易日，因此次新股上市之前的日子不会被误记为停牌。
        # 最近一次抓取失败的股票整体跳过——它的缺口可能只是漏抓。
        conn.execute(
            """WITH span AS (
                   SELECT s.id AS symbol_id, MIN(k.trade_date) AS first_day
                   FROM securities s JOIN kline k ON k.symbol_id = s.id
                   WHERE s.is_active = 1 AND k.trade_date <= ?
                     AND s.symbol NOT IN (SELECT symbol FROM download_failures)
                   GROUP BY s.id
               )
               INSERT INTO suspensions(symbol_id, trade_date)
               SELECT span.symbol_id, c.trade_date
               FROM span
               JOIN trading_calendar c ON c.trade_date BETWEEN span.first_day AND ?
               LEFT JOIN kline k ON k.symbol_id = span.symbol_id AND k.trade_date = c.trade_date
               WHERE k.trade_date IS NULL""",
            (days[-1], days[-1]),
        )
        suspended = conn.execute("SELECT COUNT(*) FROM suspensions").fetchone()[0]
    return len(days), suspended


def ensure_calendar(conn: sqlite3.Connection) -> None:
    """表为空时才重建，覆盖升级后第一次运行；已有数据时不打扰只读命令。"""
    conn.executescript(CALENDAR_SCHEMA)
    if not conn.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0]:
        refresh_calendar(conn)


def latest_trading_day(conn: sqlite3.Connection) -> str | None:
    if not calendar_schema_present(conn):
        return None
    return conn.execute("SELECT MAX(trade_date) FROM trading_calendar").fetchone()[0]


def recent_trading_days(conn: sqlite3.Connection, count: int) -> list[str]:
    """最近 count 个交易日，按时间升序返回；不足时返回实际拥有的全部。"""
    if count < 1 or not calendar_schema_present(conn):
        return []
    rows = conn.execute(
        "SELECT trade_date FROM trading_calendar ORDER BY trade_date DESC LIMIT ?", (count,)
    ).fetchall()
    return [row[0] for row in reversed(rows)]


def trading_days_between(conn: sqlite3.Connection, start: str | None, end: str | None) -> list[str]:
    """区间内的交易日；单只股票的回测靠它才能识别「哪些缺失日是交易日」。"""
    if not calendar_schema_present(conn):
        return []
    clauses, values = [], []
    if start:
        clauses.append("trade_date >= ?")
        values.append(start)
    if end:
        clauses.append("trade_date <= ?")
        values.append(end)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return [row[0] for row in conn.execute(f"SELECT trade_date FROM trading_calendar{where} ORDER BY trade_date", values)]


def suspended_days(conn: sqlite3.Connection, symbol: str, start: str, end: str) -> set[str]:
    """某股在 [start, end] 区间内的停牌交易日。"""
    if not calendar_schema_present(conn):
        return set()
    return {row[0] for row in conn.execute(
        """SELECT x.trade_date FROM suspensions x
           JOIN securities s ON s.id = x.symbol_id
           WHERE s.symbol = ? AND x.trade_date BETWEEN ? AND ?""",
        (symbol, start, end),
    )}


def suspended_symbols(conn: sqlite3.Connection) -> set[str]:
    """存在任意停牌记录的股票代码；表缺失时返回空集（消费方据此降级）。"""
    if not calendar_schema_present(conn):
        return set()
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT s.symbol FROM suspensions x JOIN securities s ON s.id = x.symbol_id"
    )}


def currently_suspended(conn: sqlite3.Connection, latest_day: str | None) -> set[str]:
    """最近一个交易日仍在停牌的股票——用于把「落后」误报从审计里摘出去。"""
    if not latest_day or not calendar_schema_present(conn):
        return set()
    return {row[0] for row in conn.execute(
        """SELECT DISTINCT s.symbol FROM suspensions x
           JOIN securities s ON s.id = x.symbol_id
           WHERE x.trade_date = ?""",
        (latest_day,),
    )}
