"""Three-stage paper workflow: evening plan, 09:45 execution, closing mark."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import sqlite3

from .backtest import limit_ratio
from .intraday import evaluate_first_bar, fetch_m15
from .paper import (PAPER_SCHEMA, PaperRebalance, _fee, _fills, _lot_size,
                    account_status, mark_account)


PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_plans (
    account_name TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    strategy_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING','EXECUTED','SKIPPED')),
    note TEXT NOT NULL,
    executed_date TEXT,
    PRIMARY KEY (account_name, signal_date)
);
CREATE TABLE IF NOT EXISTS paper_plan_items (
    account_name TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (account_name, signal_date, symbol)
);
"""


@dataclass(frozen=True)
class PaperPlan:
    account: str
    signal_date: str
    targets: tuple[str, ...]
    pending: bool
    note: str


def create_evening_plans(conn: sqlite3.Connection, signal_date: str) -> tuple[PaperPlan, ...]:
    conn.executescript(PAPER_SCHEMA + PLAN_SCHEMA)
    results: list[PaperPlan] = []
    for name, key, top_n, every in conn.execute(
        """SELECT account_name, strategy_key, top_n, rebalance_every
           FROM paper_auto_strategies WHERE enabled = 1 ORDER BY account_name"""):
        existing = conn.execute(
            "SELECT status, note FROM paper_plans WHERE account_name=? AND signal_date=?",
            (name, signal_date)).fetchone()
        if existing:
            targets = tuple(row[0] for row in conn.execute(
                "SELECT symbol FROM paper_plan_items WHERE account_name=? AND signal_date=? ORDER BY rank",
                (name, signal_date)))
            results.append(PaperPlan(name, signal_date, targets, existing[0] == "PENDING", existing[1]))
            continue
        last = conn.execute("SELECT MAX(trade_date) FROM paper_rebalances WHERE account_name=?", (name,)).fetchone()[0]
        if last:
            elapsed = conn.execute(
                "SELECT COUNT(*) FROM trading_calendar WHERE trade_date>? AND trade_date<=?", (last, signal_date)
            ).fetchone()[0]
            if elapsed < every:
                results.append(PaperPlan(name, signal_date, (), False,
                                         f"距上次调仓 {elapsed}/{every} 个交易日，明日不调仓"))
                continue
        rows = conn.execute(
            """SELECT symbol FROM candidate_snapshots WHERE strategy_key=? AND as_of=?
               ORDER BY momentum DESC, symbol LIMIT ?""", (key, signal_date, top_n)).fetchall()
        targets = tuple(row[0] for row in rows)
        if not targets:
            results.append(PaperPlan(name, signal_date, (), False, "当日没有可用候选快照"))
            continue
        note = f"下一交易日09:45观察并模拟调仓：{','.join(targets)}"
        with conn:
            conn.execute(
                """UPDATE paper_plans SET status='SKIPPED', note='已被更新的候选快照替代'
                   WHERE account_name=? AND status='PENDING'""", (name,))
            conn.execute("INSERT INTO paper_plans VALUES (?,?,?,'PENDING',?,NULL)", (name, signal_date, key, note))
            conn.executemany("INSERT INTO paper_plan_items VALUES (?,?,?,?)",
                             [(name, signal_date, symbol, rank) for rank, symbol in enumerate(targets, 1)])
        results.append(PaperPlan(name, signal_date, targets, True, note))
    return tuple(results)


def close_enabled_accounts(conn: sqlite3.Connection, as_of: str) -> tuple[object, ...]:
    conn.executescript(PAPER_SCHEMA)
    return tuple(mark_account(conn, row[0], as_of) for row in conn.execute(
        "SELECT account_name FROM paper_auto_strategies WHERE enabled=1 ORDER BY account_name"))


def execute_pending_plans(conn: sqlite3.Connection, trade_date: str | None = None) -> tuple[PaperRebalance, ...]:
    trade_date = trade_date or date.today().isoformat()
    conn.executescript(PAPER_SCHEMA + PLAN_SCHEMA)
    outputs: list[PaperRebalance] = []
    plans = conn.execute(
        """SELECT p.account_name, p.signal_date, s.commission_rate, s.minimum_commission,
                  s.stamp_duty_rate, s.slippage_bps
           FROM paper_plans p JOIN paper_auto_strategies s ON s.account_name=p.account_name
           WHERE p.status='PENDING' AND p.signal_date < ? ORDER BY p.account_name, p.signal_date DESC""",
        (trade_date,)).fetchall()
    seen: set[str] = set()
    for name, signal_date, commission, minimum, stamp, slippage_bps in plans:
        if name in seen:
            continue
        seen.add(name)
        existing = conn.execute("SELECT 1 FROM paper_rebalances WHERE account_name=? AND trade_date=?",
                                (name, trade_date)).fetchone()
        if existing:
            outputs.append(PaperRebalance(name, trade_date, signal_date, False, 0, 0, 0,
                                          "当日已执行，未重复成交", account_status(conn, name, signal_date),
                                          _fills(conn, name, trade_date)))
            continue
        targets = tuple(row[0] for row in conn.execute(
            "SELECT symbol FROM paper_plan_items WHERE account_name=? AND signal_date=? ORDER BY rank",
            (name, signal_date)))
        holdings = dict(conn.execute("SELECT symbol,shares FROM paper_positions WHERE account_name=?", (name,)))
        bars = {}
        for symbol in set(targets) | set(holdings):
            recent = fetch_m15(symbol)
            bars[symbol] = next((bar for bar in recent if bar.bar_time == f"{trade_date} 09:45"), None)
        # No 09:45 bar means holiday, endpoint delay, or data failure: keep the plan pending.
        if not any(bars.values()):
            continue
        cash = float(conn.execute("SELECT cash FROM paper_accounts WHERE name=?", (name,)).fetchone()[0])
        buys = sells = blocked = 0
        slip = float(slippage_bps) / 10_000
        with conn:
            for symbol, shares in list(holdings.items()):
                if symbol in targets:
                    continue
                bar = bars.get(symbol)
                previous = conn.execute("SELECT close FROM daily_kline WHERE symbol=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
                                        (symbol, signal_date)).fetchone()
                if not bar or not previous or bar.close / float(previous[0]) - 1 <= -limit_ratio(symbol) + .001:
                    blocked += 1
                    continue
                price = bar.close * (1 - slip); value = shares * price
                fee, tax = _fee(value, commission, minimum), value * stamp
                cash += value - fee - tax
                conn.execute("DELETE FROM paper_positions WHERE account_name=? AND symbol=?", (name, symbol))
                conn.execute("INSERT INTO paper_trades(account_name,trade_date,snapshot_date,symbol,side,shares,price,fee,tax) VALUES (?,?,?,?, 'SELL',?,?,?,?)",
                             (name, trade_date, signal_date, symbol, shares, price, fee, tax))
                sells += 1
            missing = [symbol for symbol in targets if symbol not in holdings]
            for index, symbol in enumerate(missing):
                bar = bars.get(symbol)
                previous = conn.execute("SELECT close FROM daily_kline WHERE symbol=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
                                        (symbol, signal_date)).fetchone()
                if not bar or not previous:
                    blocked += 1; continue
                decision = evaluate_first_bar(symbol, bar.open, float(previous[0]), bar)
                if not decision.accepted or bar.close / float(previous[0]) - 1 >= limit_ratio(symbol) - .001:
                    blocked += 1; continue
                allocation = cash / max(1, len(missing) - index)
                price = bar.close * (1 + slip); lot = _lot_size(symbol)
                shares = int((allocation - minimum) / (price * (1 + commission)) / lot) * lot
                if shares < lot: continue
                value = shares * price; fee = _fee(value, commission, minimum)
                if value + fee > cash: continue
                cash -= value + fee
                conn.execute("INSERT INTO paper_positions VALUES (?,?,?,?) ON CONFLICT(account_name,symbol) DO UPDATE SET shares=excluded.shares,average_cost=excluded.average_cost",
                             (name, symbol, shares, (value + fee) / shares))
                conn.execute("INSERT INTO paper_trades(account_name,trade_date,snapshot_date,symbol,side,shares,price,fee,tax) VALUES (?,?,?,?, 'BUY',?,?,?,0)",
                             (name, trade_date, signal_date, symbol, shares, price, fee))
                buys += 1
            note = f"09:45模拟执行：买入{buys}，卖出{sells}，过滤/受阻{blocked}"
            conn.execute("UPDATE paper_accounts SET cash=? WHERE name=?", (cash, name))
            conn.execute("INSERT INTO paper_rebalances VALUES (?,?,?,?)", (name, trade_date, signal_date, note))
            conn.execute("UPDATE paper_plans SET status='EXECUTED',executed_date=?,note=? WHERE account_name=? AND signal_date=?",
                         (trade_date, note, name, signal_date))
        outputs.append(PaperRebalance(name, trade_date, signal_date, True, buys, sells, blocked, note,
                                      account_status(conn, name, signal_date), _fills(conn, name, trade_date)))
    return tuple(outputs)
