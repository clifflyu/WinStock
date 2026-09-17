"""Current-day candidate universe generation; it does not issue trade orders."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from .audit import audit_database


@dataclass(frozen=True)
class Candidate:
    symbol: str
    name: str
    as_of: str
    momentum: float
    average_amount: float
    history_days: int


def momentum_candidates(
    conn: sqlite3.Connection, as_of: str | None = None, top_n: int = 10, lookback: int = 20,
    min_history: int = 250, min_avg_amount: float = 20_000_000,
) -> list[Candidate]:
    """Rank current eligible A shares by trailing return using only completed data."""
    if top_n < 1 or lookback < 1 or min_history < lookback + 1 or min_avg_amount < 0:
        raise ValueError("参数无效：min_history 至少为 lookback + 1，数量和成交额门槛不能为负")
    as_of = as_of or audit_database(conn).latest_day
    if not as_of:
        raise ValueError("没有完整覆盖的交易日，无法生成候选池")
    required = min_history
    rows = conn.execute(
        """WITH recent AS (
               SELECT d.symbol, s.name, d.trade_date, d.close, d.amount,
                      ROW_NUMBER() OVER (PARTITION BY d.symbol ORDER BY d.trade_date DESC) AS rn
               FROM daily_kline d JOIN securities s ON s.symbol = d.symbol
               WHERE s.is_active = 1 AND d.trade_date <= ?
           )
           SELECT symbol, name, trade_date, close, amount, rn FROM recent
           WHERE rn <= ? ORDER BY symbol, trade_date DESC""", (as_of, required)
    )
    grouped: dict[tuple[str, str], list[tuple[str, float, float | None]]] = {}
    for symbol, name, day, close, amount, _ in rows:
        grouped.setdefault((symbol, name), []).append((day, float(close), float(amount) if amount is not None else None))
    result: list[Candidate] = []
    for (symbol, name), bars in grouped.items():
        if "ST" in name.upper() or len(bars) < min_history or bars[0][0] != as_of:
            continue
        recent = bars[:lookback]
        if any(amount is None for _, _, amount in recent):
            continue
        average_amount = sum(amount for _, _, amount in recent if amount is not None) / lookback
        if average_amount < min_avg_amount:
            continue
        momentum = bars[0][1] / bars[lookback][1] - 1
        result.append(Candidate(symbol, name, as_of, momentum, average_amount, len(bars)))
    return sorted(result, key=lambda item: (-item.momentum, item.symbol))[:top_n]
