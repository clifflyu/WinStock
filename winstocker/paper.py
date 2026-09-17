"""Local-only paper portfolio storage.  This module has no broker or network code."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from .audit import audit_database


PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_accounts (
    name TEXT PRIMARY KEY,
    initial_cash REAL NOT NULL CHECK(initial_cash > 0),
    cash REAL NOT NULL CHECK(cash >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS paper_positions (
    account_name TEXT NOT NULL REFERENCES paper_accounts(name),
    symbol TEXT NOT NULL,
    shares INTEGER NOT NULL CHECK(shares >= 0),
    average_cost REAL NOT NULL CHECK(average_cost >= 0),
    PRIMARY KEY (account_name, symbol)
);
CREATE TABLE IF NOT EXISTS paper_equity (
    account_name TEXT NOT NULL REFERENCES paper_accounts(name),
    trade_date TEXT NOT NULL,
    cash REAL NOT NULL,
    market_value REAL NOT NULL,
    total_value REAL NOT NULL,
    PRIMARY KEY (account_name, trade_date)
);
"""


@dataclass(frozen=True)
class PaperStatus:
    account: str
    as_of: str
    initial_cash: float
    cash: float
    market_value: float
    total_value: float
    positions: int


def create_account(conn: sqlite3.Connection, name: str, initial_cash: float) -> None:
    if not name.strip() or initial_cash <= 0:
        raise ValueError("账户名不能为空，初始资金必须大于 0")
    conn.executescript(PAPER_SCHEMA)
    try:
        with conn:
            conn.execute("INSERT INTO paper_accounts(name, initial_cash, cash) VALUES (?, ?, ?)", (name, initial_cash, initial_cash))
    except sqlite3.IntegrityError as error:
        raise ValueError(f"模拟账户 {name} 已存在") from error


def account_status(conn: sqlite3.Connection, name: str, as_of: str | None = None) -> PaperStatus:
    conn.executescript(PAPER_SCHEMA)
    account = conn.execute("SELECT initial_cash, cash FROM paper_accounts WHERE name = ?", (name,)).fetchone()
    if not account:
        raise ValueError(f"未找到模拟账户 {name}")
    as_of = as_of or audit_database(conn).latest_day
    if not as_of:
        raise ValueError("没有完整覆盖交易日，无法估值")
    positions = conn.execute("SELECT symbol, shares FROM paper_positions WHERE account_name = ? AND shares > 0", (name,)).fetchall()
    market_value = 0.0
    for symbol, shares in positions:
        close = conn.execute("SELECT close FROM daily_kline WHERE symbol = ? AND trade_date <= ? ORDER BY trade_date DESC LIMIT 1", (symbol, as_of)).fetchone()
        if not close:
            raise ValueError(f"{symbol} 没有可用的估值价格")
        market_value += shares * float(close[0])
    return PaperStatus(name, as_of, float(account[0]), float(account[1]), market_value, float(account[1]) + market_value, len(positions))


def mark_account(conn: sqlite3.Connection, name: str, as_of: str | None = None) -> PaperStatus:
    status = account_status(conn, name, as_of)
    with conn:
        conn.execute(
            """INSERT INTO paper_equity(account_name, trade_date, cash, market_value, total_value)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_name, trade_date) DO UPDATE SET
               cash=excluded.cash, market_value=excluded.market_value, total_value=excluded.total_value""",
            (status.account, status.as_of, status.cash, status.market_value, status.total_value),
        )
    return status
