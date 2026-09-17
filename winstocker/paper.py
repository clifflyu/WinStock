"""Local-only paper portfolio storage.  This module has no broker or network code."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from .audit import audit_database
from .backtest import limit_ratio
from .snapshots import DEFAULT_STRATEGY_KEY, previous_snapshot


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
CREATE TABLE IF NOT EXISTS paper_auto_strategies (
    account_name TEXT PRIMARY KEY REFERENCES paper_accounts(name),
    strategy_key TEXT NOT NULL,
    top_n INTEGER NOT NULL CHECK(top_n > 0),
    rebalance_every INTEGER NOT NULL CHECK(rebalance_every > 0),
    commission_rate REAL NOT NULL,
    minimum_commission REAL NOT NULL,
    stamp_duty_rate REAL NOT NULL,
    slippage_bps REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS paper_rebalances (
    account_name TEXT NOT NULL REFERENCES paper_accounts(name),
    trade_date TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY (account_name, trade_date)
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY,
    account_name TEXT NOT NULL REFERENCES paper_accounts(name),
    trade_date TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    shares INTEGER NOT NULL CHECK(shares > 0),
    price REAL NOT NULL CHECK(price > 0),
    fee REAL NOT NULL CHECK(fee >= 0),
    tax REAL NOT NULL CHECK(tax >= 0),
    UNIQUE(account_name, trade_date, symbol, side)
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


@dataclass(frozen=True)
class PaperFill:
    symbol: str
    side: str
    shares: int
    price: float
    fee: float
    tax: float


@dataclass(frozen=True)
class PaperRebalance:
    account: str
    trade_date: str
    snapshot_date: str | None
    executed: bool
    buys: int
    sells: int
    blocked: int
    note: str
    status: PaperStatus
    fills: tuple[PaperFill, ...] = ()


def _fills(conn: sqlite3.Connection, name: str, trade_date: str) -> tuple[PaperFill, ...]:
    return tuple(PaperFill(str(symbol), str(side), int(shares), float(price), float(fee), float(tax))
                 for symbol, side, shares, price, fee, tax in conn.execute(
                     """SELECT symbol, side, shares, price, fee, tax FROM paper_trades
                        WHERE account_name = ? AND trade_date = ? ORDER BY id""",
                     (name, trade_date)))


def create_account(conn: sqlite3.Connection, name: str, initial_cash: float) -> None:
    if not name.strip() or initial_cash <= 0:
        raise ValueError("账户名不能为空，初始资金必须大于 0")
    conn.executescript(PAPER_SCHEMA)
    try:
        with conn:
            conn.execute("INSERT INTO paper_accounts(name, initial_cash, cash) VALUES (?, ?, ?)", (name, initial_cash, initial_cash))
    except sqlite3.IntegrityError as error:
        raise ValueError(f"模拟账户 {name} 已存在") from error


def enable_auto_strategy(conn: sqlite3.Connection, name: str, strategy_key: str = DEFAULT_STRATEGY_KEY,
                         top_n: int = 3, rebalance_every: int = 20,
                         commission_rate: float = 0.0003, minimum_commission: float = 5,
                         stamp_duty_rate: float = 0.0005, slippage_bps: float = 5) -> None:
    conn.executescript(PAPER_SCHEMA)
    if not conn.execute("SELECT 1 FROM paper_accounts WHERE name = ?", (name,)).fetchone():
        raise ValueError(f"未找到模拟账户 {name}")
    if top_n < 1 or rebalance_every < 1:
        raise ValueError("top_n 和 rebalance_every 必须为正整数")
    with conn:
        conn.execute(
            """INSERT INTO paper_auto_strategies
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(account_name) DO UPDATE SET strategy_key=excluded.strategy_key,
                 top_n=excluded.top_n, rebalance_every=excluded.rebalance_every,
                 commission_rate=excluded.commission_rate, minimum_commission=excluded.minimum_commission,
                 stamp_duty_rate=excluded.stamp_duty_rate, slippage_bps=excluded.slippage_bps, enabled=1""",
            (name, strategy_key, top_n, rebalance_every, commission_rate, minimum_commission,
             stamp_duty_rate, slippage_bps),
        )


def _lot_size(symbol: str) -> int:
    return 200 if symbol.startswith(("688", "689")) else 100


def _fee(value: float, rate: float, minimum: float) -> float:
    return max(value * rate, minimum)


def rebalance_account(conn: sqlite3.Connection, name: str, trade_date: str) -> PaperRebalance:
    """按上一快照在当日开盘模拟调仓；同一账户同一天严格幂等。"""
    conn.executescript(PAPER_SCHEMA)
    config = conn.execute(
        """SELECT strategy_key, top_n, rebalance_every, commission_rate, minimum_commission,
                  stamp_duty_rate, slippage_bps FROM paper_auto_strategies
           WHERE account_name = ? AND enabled = 1""", (name,),
    ).fetchone()
    if not config:
        raise ValueError(f"模拟账户 {name} 未启用自动策略")
    existing = conn.execute(
        "SELECT snapshot_date, note FROM paper_rebalances WHERE account_name = ? AND trade_date = ?",
        (name, trade_date),
    ).fetchone()
    if existing:
        return PaperRebalance(name, trade_date, existing[0], False, 0, 0, 0,
                              "当日已经处理，未重复成交", account_status(conn, name, trade_date),
                              _fills(conn, name, trade_date))
    strategy_key, top_n, every, commission, minimum, stamp, slippage_bps = config
    snapshot_date, ranked = previous_snapshot(conn, trade_date, strategy_key)
    if not snapshot_date or not ranked:
        return PaperRebalance(name, trade_date, snapshot_date, False, 0, 0, 0,
                              "没有上一交易日候选快照", account_status(conn, name, trade_date))
    last = conn.execute(
        "SELECT MAX(trade_date) FROM paper_rebalances WHERE account_name = ?", (name,),
    ).fetchone()[0]
    if last:
        elapsed = conn.execute(
            "SELECT COUNT(*) FROM trading_calendar WHERE trade_date > ? AND trade_date <= ?",
            (last, trade_date),
        ).fetchone()[0]
        if elapsed < every:
            return PaperRebalance(name, trade_date, snapshot_date, False, 0, 0, 0,
                                  f"距上次调仓仅 {elapsed} 个交易日，等待满 {every} 日",
                                  mark_account(conn, name, trade_date))

    targets = tuple(ranked[:top_n])
    cash = float(conn.execute("SELECT cash FROM paper_accounts WHERE name = ?", (name,)).fetchone()[0])
    holdings = dict(conn.execute(
        "SELECT symbol, shares FROM paper_positions WHERE account_name = ? AND shares > 0", (name,),
    ))
    buys = sells = blocked = 0
    slip = slippage_bps / 10_000
    with conn:
        for symbol, shares in list(holdings.items()):
            if symbol in targets:
                continue
            bar = conn.execute(
                "SELECT open FROM daily_kline WHERE symbol = ? AND trade_date = ?", (symbol, trade_date),
            ).fetchone()
            reference = conn.execute(
                "SELECT close FROM daily_kline WHERE symbol = ? AND trade_date < ? ORDER BY trade_date DESC LIMIT 1",
                (symbol, trade_date),
            ).fetchone()
            if not bar or not reference or float(bar[0]) / float(reference[0]) - 1 <= -limit_ratio(symbol) + 0.001:
                blocked += 1
                continue
            price = float(bar[0]) * (1 - slip)
            value = shares * price
            fee, tax = _fee(value, commission, minimum), value * stamp
            cash += value - fee - tax
            conn.execute("DELETE FROM paper_positions WHERE account_name = ? AND symbol = ?", (name, symbol))
            conn.execute(
                """INSERT INTO paper_trades(account_name, trade_date, snapshot_date, symbol, side, shares, price, fee, tax)
                   VALUES (?, ?, ?, ?, 'SELL', ?, ?, ?, ?)""",
                (name, trade_date, snapshot_date, symbol, shares, price, fee, tax),
            )
            sells += 1

        missing = [symbol for symbol in targets if symbol not in holdings]
        for index, symbol in enumerate(missing):
            bar = conn.execute(
                "SELECT open FROM daily_kline WHERE symbol = ? AND trade_date = ?", (symbol, trade_date),
            ).fetchone()
            reference = conn.execute(
                "SELECT close FROM daily_kline WHERE symbol = ? AND trade_date < ? ORDER BY trade_date DESC LIMIT 1",
                (symbol, trade_date),
            ).fetchone()
            if not bar or not reference or float(bar[0]) / float(reference[0]) - 1 >= limit_ratio(symbol) - 0.001:
                blocked += 1
                continue
            allocation = cash / max(1, len(missing) - index)
            price = float(bar[0]) * (1 + slip)
            lot = _lot_size(symbol)
            shares = int((allocation - minimum) / (price * (1 + commission)) / lot) * lot
            if shares < lot:
                continue
            value = shares * price
            fee = _fee(value, commission, minimum)
            if value + fee > cash:
                continue
            cash -= value + fee
            conn.execute(
                """INSERT INTO paper_positions(account_name, symbol, shares, average_cost)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_name, symbol) DO UPDATE SET shares=excluded.shares,
                     average_cost=excluded.average_cost""", (name, symbol, shares, (value + fee) / shares),
            )
            conn.execute(
                """INSERT INTO paper_trades(account_name, trade_date, snapshot_date, symbol, side, shares, price, fee, tax)
                   VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, 0)""",
                (name, trade_date, snapshot_date, symbol, shares, price, fee),
            )
            buys += 1
        note = f"快照 {snapshot_date}；买入 {buys}，卖出 {sells}，受阻 {blocked}"
        conn.execute("UPDATE paper_accounts SET cash = ? WHERE name = ?", (cash, name))
        conn.execute("INSERT INTO paper_rebalances VALUES (?, ?, ?, ?)",
                     (name, trade_date, snapshot_date, note))
    status = mark_account(conn, name, trade_date)
    return PaperRebalance(name, trade_date, snapshot_date, True, buys, sells, blocked, note, status,
                          _fills(conn, name, trade_date))


def run_enabled_strategies(conn: sqlite3.Connection, trade_date: str) -> tuple[PaperRebalance, ...]:
    conn.executescript(PAPER_SCHEMA)
    names = [row[0] for row in conn.execute(
        "SELECT account_name FROM paper_auto_strategies WHERE enabled = 1 ORDER BY account_name")]
    return tuple(rebalance_account(conn, name, trade_date) for name in names)


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
