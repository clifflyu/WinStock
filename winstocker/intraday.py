"""Forward-only 15-minute execution experiment; never routes broker orders."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import sqlite3
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .snapshots import DEFAULT_STRATEGY_KEY, previous_snapshot


M15_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
INTRADAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_bars (
    symbol TEXT NOT NULL,
    bar_time TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    open REAL NOT NULL,
    close REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    volume REAL,
    PRIMARY KEY (symbol, bar_time, interval_minutes)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS execution_experiments (
    strategy_key TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    baseline_open REAL NOT NULL,
    delayed_price REAL,
    accepted INTEGER NOT NULL,
    reason TEXT NOT NULL,
    gap_return REAL,
    first_bar_return REAL,
    first_bar_range REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (strategy_key, trade_date, symbol)
);
"""


@dataclass(frozen=True)
class MinuteBar:
    symbol: str
    bar_time: str
    open: float
    close: float
    high: float
    low: float
    volume: float | None


@dataclass(frozen=True)
class ExecutionDecision:
    symbol: str
    accepted: bool
    reason: str
    baseline_open: float
    delayed_price: float | None
    gap_return: float | None
    first_bar_return: float | None
    first_bar_range: float | None


@dataclass(frozen=True)
class IntradayExperiment:
    signal_date: str | None
    trade_date: str
    decisions: tuple[ExecutionDecision, ...]
    error: str | None = None


def tencent_code(symbol: str) -> str:
    return f'{"sh" if symbol.startswith(("5", "6", "9")) else "sz"}{symbol}'


def fetch_m15(symbol: str, count: int = 80) -> list[MinuteBar]:
    code = tencent_code(symbol)
    url = f"{M15_URL}?{urlencode({'param': f'{code},m15,,{count}'})}"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=20) as response:
        payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    rows = ((payload.get("data") or {}).get(code) or {}).get("m15") or []
    result: list[MinuteBar] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        moment = datetime.strptime(str(row[0]), "%Y%m%d%H%M").strftime("%Y-%m-%d %H:%M")
        result.append(MinuteBar(symbol, moment, float(row[1]), float(row[2]),
                                float(row[3]), float(row[4]), float(row[5])))
    return result


def evaluate_first_bar(symbol: str, baseline_open: float, previous_close: float,
                       bar: MinuteBar | None, max_gap: float = 0.03,
                       max_move: float = 0.02, max_range: float = 0.04) -> ExecutionDecision:
    if bar is None:
        return ExecutionDecision(symbol, False, "缺少09:45分钟K", baseline_open, None,
                                 None, None, None)
    gap = bar.open / previous_close - 1
    move = bar.close / bar.open - 1
    spread = (bar.high - bar.low) / previous_close
    reasons: list[str] = []
    if gap > max_gap:
        reasons.append(f"高开{gap:.2%}超过{max_gap:.0%}")
    if abs(move) > max_move:
        reasons.append(f"首15分钟涨跌{move:+.2%}超过±{max_move:.0%}")
    if spread > max_range:
        reasons.append(f"首15分钟振幅{spread:.2%}超过{max_range:.0%}")
    accepted = not reasons
    return ExecutionDecision(symbol, accepted, "通过" if accepted else "；".join(reasons),
                             baseline_open, bar.close if accepted else None, gap, move, spread)


def run_execution_experiment(conn: sqlite3.Connection, trade_date: str, top_n: int = 3,
                             fetcher: Callable[[str], list[MinuteBar]] = fetch_m15,
                             strategy_key: str = DEFAULT_STRATEGY_KEY) -> IntradayExperiment:
    """Record a same-universe open-vs-09:45 shadow decision using only completed bars."""
    conn.executescript(INTRADAY_SCHEMA)
    signal_date, ranked = previous_snapshot(conn, trade_date, strategy_key)
    if not signal_date or not ranked:
        return IntradayExperiment(signal_date, trade_date, (), "没有上一交易日候选快照")
    decisions: list[ExecutionDecision] = []
    for symbol in ranked[:top_n]:
        daily = conn.execute(
            "SELECT open FROM daily_kline WHERE symbol = ? AND trade_date = ?", (symbol, trade_date),
        ).fetchone()
        previous = conn.execute(
            "SELECT close FROM daily_kline WHERE symbol = ? AND trade_date < ? ORDER BY trade_date DESC LIMIT 1",
            (symbol, trade_date),
        ).fetchone()
        if not daily or not previous:
            continue
        bars = fetcher(symbol)
        with conn:
            conn.executemany(
                """INSERT INTO minute_bars VALUES (?, ?, 15, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, bar_time, interval_minutes) DO UPDATE SET
                     open=excluded.open, close=excluded.close, high=excluded.high,
                     low=excluded.low, volume=excluded.volume""",
                [(item.symbol, item.bar_time, item.open, item.close, item.high, item.low, item.volume)
                 for item in bars],
            )
        target_time = f"{trade_date} 09:45"
        first = next((item for item in bars if item.bar_time == target_time), None)
        decision = evaluate_first_bar(symbol, float(daily[0]), float(previous[0]), first)
        decisions.append(decision)
        with conn:
            conn.execute(
                """INSERT INTO execution_experiments
                   (strategy_key, signal_date, trade_date, symbol, baseline_open, delayed_price,
                    accepted, reason, gap_return, first_bar_return, first_bar_range)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(strategy_key, trade_date, symbol) DO UPDATE SET
                     baseline_open=excluded.baseline_open, delayed_price=excluded.delayed_price,
                     accepted=excluded.accepted, reason=excluded.reason, gap_return=excluded.gap_return,
                     first_bar_return=excluded.first_bar_return, first_bar_range=excluded.first_bar_range""",
                (strategy_key, signal_date, trade_date, symbol, decision.baseline_open,
                 decision.delayed_price, int(decision.accepted), decision.reason,
                 decision.gap_return, decision.first_bar_return, decision.first_bar_range),
            )
    return IntradayExperiment(signal_date, trade_date, tuple(decisions))
