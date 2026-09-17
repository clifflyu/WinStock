"""Small, explicit daily-bar backtester for research use.

Signals are calculated at yesterday's close and orders are filled at today's
open.  It deliberately supports one long-only A-share position: this makes the
execution assumptions inspectable before strategies are made more complex.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Bar:
    day: str
    open: float
    close: float
    amount: float | None = None


@dataclass
class BacktestResult:
    symbol: str
    start: str
    end: str
    initial_cash: float
    final_value: float
    total_return: float
    annualized_return: float | None
    max_drawdown: float
    trades: int
    blocked_buys: int
    blocked_sells: int
    suspension_days: int = 0


@dataclass
class RotationResult:
    symbols: tuple[str, ...]
    start: str
    end: str
    initial_cash: float
    final_value: float
    total_return: float
    annualized_return: float | None
    max_drawdown: float
    trades: int
    rebalances: int
    blocked_buys: int
    blocked_sells: int
    # 持仓期间「该股当日无行情」的天数：这段里净值被按停牌前收盘挂着，
    # 曲线会走成水平线，看起来像低波动，实际是卖不掉。
    suspension_days: int = 0
    suspension_blocked_sells: int = 0


@dataclass
class WalkForwardResult:
    split_day: str
    train: RotationResult
    validation: RotationResult


def load_bars(conn: sqlite3.Connection, symbol: str, start: str | None, end: str | None) -> list[Bar]:
    clauses, values = ["symbol = ?", "open IS NOT NULL", "close IS NOT NULL", "open > 0", "close > 0"], [symbol]
    if start:
        clauses.append("trade_date >= ?")
        values.append(start)
    if end:
        clauses.append("trade_date <= ?")
        values.append(end)
    rows = conn.execute(
        "SELECT trade_date, open, close, amount FROM daily_kline WHERE " + " AND ".join(clauses) + " ORDER BY trade_date", values
    )
    return [Bar(day, float(open_), float(close), float(amount) if amount is not None else None) for day, open_, close, amount in rows]


def load_panel(conn: sqlite3.Connection, symbols: Iterable[str], start: str | None, end: str | None) -> dict[str, dict[str, Bar]]:
    """Load a caller-specified universe.  The explicit universe prevents accidental survivor bias claims."""
    symbols = tuple(dict.fromkeys(symbols))
    if not symbols:
        raise ValueError("至少提供一只股票")
    placeholders = ", ".join("?" for _ in symbols)
    clauses, values = [f"symbol IN ({placeholders})", "open IS NOT NULL", "close IS NOT NULL", "open > 0", "close > 0"], list(symbols)
    if start:
        clauses.append("trade_date >= ?")
        values.append(start)
    if end:
        clauses.append("trade_date <= ?")
        values.append(end)
    rows = conn.execute(
        "SELECT symbol, trade_date, open, close, amount FROM daily_kline WHERE " + " AND ".join(clauses) + " ORDER BY trade_date", values
    )
    panel = {symbol: {} for symbol in symbols}
    for symbol, day, open_, close, amount in rows:
        panel[symbol][day] = Bar(day, float(open_), float(close), float(amount) if amount is not None else None)
    return panel


def limit_ratio(symbol: str) -> float:
    """Regular daily price limit; IPO no-limit days and historical ST flags need extra data."""
    return 0.20 if symbol.startswith(("300", "301", "688", "689")) else 0.10


def _fee(value: float, commission_rate: float, minimum_commission: float) -> float:
    return max(value * commission_rate, minimum_commission)


def dual_ma_backtest(
    bars: Iterable[Bar], symbol: str, fast: int = 20, slow: int = 60, initial_cash: float = 100_000,
    commission_rate: float = 0.0003, minimum_commission: float = 5, stamp_duty_rate: float = 0.0005,
    slippage_bps: float = 5, trading_days: Sequence[str] | None = None,
) -> BacktestResult:
    """Backtest a golden/death-cross strategy with all-in, board-lot A-share orders."""
    bars = list(bars)
    if fast < 1 or slow <= fast:
        raise ValueError("slow 必须大于 fast，且 fast 为正整数")
    if len(bars) <= slow:
        raise ValueError(f"数据不足：双均线至少需要 {slow + 1} 根日线")
    cash, shares, trades, blocked_buys, blocked_sells = initial_cash, 0, 0, 0, 0
    equity_curve: list[float] = []
    states: list[bool] = []
    position = False
    limit = limit_ratio(symbol)
    slip = slippage_bps / 10_000

    # At i, the decision only sees closes through i-1 and fills at i's open.
    for i, bar in enumerate(bars):
        if i > slow:
            fast_ma = sum(item.close for item in bars[i - fast:i]) / fast
            slow_ma = sum(item.close for item in bars[i - slow:i]) / slow
            should_hold = fast_ma > slow_ma
            previous_close = bars[i - 1].close
            opening_return = bar.open / previous_close - 1
            at_up_limit = opening_return >= limit - 0.001
            at_down_limit = opening_return <= -limit + 0.001
            if should_hold and not position:
                if at_up_limit:
                    blocked_buys += 1
                else:
                    price = bar.open * (1 + slip)
                    # Solve cash >= shares * price + max(shares*price*rate, min fee).
                    affordable = int((cash - minimum_commission) / (price * (1 + commission_rate)) / 100) * 100
                    if affordable >= 100:
                        value = affordable * price
                        cash -= value + _fee(value, commission_rate, minimum_commission)
                        shares, position, trades = affordable, True, trades + 1
            elif not should_hold and position:
                if at_down_limit:
                    blocked_sells += 1
                else:
                    price = bar.open * (1 - slip)
                    value = shares * price
                    cash += value - _fee(value, commission_rate, minimum_commission) - value * stamp_duty_rate
                    shares, position, trades = 0, False, trades + 1
        states.append(position)
        equity_curve.append(cash + shares * bar.close)

    final_value = equity_curve[-1]
    high_water = equity_curve[0]
    max_drawdown = 0.0
    for value in equity_curve:
        high_water = max(high_water, value)
        max_drawdown = min(max_drawdown, value / high_water - 1)
    years = len(bars) / 252
    total_return = final_value / initial_cash - 1
    annualized = (final_value / initial_cash) ** (1 / years) - 1 if years else None
    # 单只股票无法自行判断「哪些缺失日是交易日」，需要调用方传入交易日历。
    # 两根 K 线之间夹着的交易日就是停牌日；只有当时确实持仓的才算暴露——
    # 与轮动回测的口径保持一致（那段净值被按停牌前收盘挂着，曲线走平，
    # 看起来像低波动，实际是无法卖出）。
    suspended = 0
    if trading_days:
        positions = {day: index for index, day in enumerate(trading_days)}
        for index, bar in enumerate(bars[:-1]):
            if not states[index] or bar.day not in positions:
                continue
            following = bars[index + 1].day
            if following in positions:
                suspended += positions[following] - positions[bar.day] - 1
    return BacktestResult(symbol, bars[0].day, bars[-1].day, initial_cash, final_value, total_return,
                          annualized, max_drawdown, trades, blocked_buys, blocked_sells, suspended)


def momentum_rotation_backtest(
    panel: dict[str, dict[str, Bar]], top_n: int = 5, lookback: int = 20, rebalance_every: int = 20,
    initial_cash: float = 100_000, commission_rate: float = 0.0003, minimum_commission: float = 5,
    stamp_duty_rate: float = 0.0005, slippage_bps: float = 5, min_history: int = 0, min_avg_amount: float = 0,
    initial_history: int = 0,
) -> RotationResult:
    """Equal-weight top-momentum rotation using only data available before each rebalance open."""
    if top_n < 1 or lookback < 1 or rebalance_every < 1 or min_history < 0 or min_avg_amount < 0 or initial_history < 0:
        raise ValueError("策略窗口为正整数，股票历史与成交额门槛不能为负数")
    days = sorted({day for bars in panel.values() for day in bars})
    if len(days) <= lookback + 1:
        raise ValueError(f"数据不足：动量策略至少需要 {lookback + 2} 个交易日")
    symbols = tuple(panel)
    cash, holdings, trades, rebalances, blocked_buys, blocked_sells = initial_cash, {}, 0, 0, 0, 0
    suspension_days = suspension_blocked_sells = 0
    curve: list[float] = []
    last_close: dict[str, float] = {}
    # 涨跌停判定要用的参考价必须是「今日之前」的收盘价。last_close 在当日开盘前就被
    # 更新成当日收盘价，直接拿它算就是用了尚未发生的信息——复牌当天正是退出触发的
    # 时候，所以这条特别容易踩。reference_close 因此要单独维护。
    reference_close: dict[str, float] = {}
    pending_exits: set[str] = set()
    slip = slippage_bps / 10_000

    for i, day in enumerate(days):
        # 停牌期间卖不掉的仓位在此处等复牌：复牌首个交易日按真实开盘价成交。
        for symbol in sorted(pending_exits):
            bars = panel[symbol]
            reference = reference_close.get(symbol)
            if day not in bars or reference is None:
                continue
            shares = holdings.get(symbol)
            if not shares:
                pending_exits.discard(symbol)
                continue
            bar = bars[day]
            if bar.open / reference - 1 <= -limit_ratio(symbol) + 0.001:
                blocked_sells += 1  # 复牌即跌停，仍然卖不掉
                continue
            value = shares * bar.open * (1 - slip)
            cash += value - _fee(value, commission_rate, minimum_commission) - value * stamp_duty_rate
            del holdings[symbol]
            pending_exits.discard(symbol)
            trades += 1
        for symbol, bars in panel.items():
            if day in bars:
                reference_close[symbol] = last_close.get(symbol, bars[day].close)
                last_close[symbol] = bars[day].close
        # 首个可调仓日是 lookback + 1：动量要覆盖 lookback 个交易日区间（与
        # candidates 的 bars[0]/bars[lookback] 同口径），且信号必须止于昨日——
        # 当日开盘成交，排名里不能出现当日收盘价。
        if i >= lookback + 1 and (i - lookback - 1) % rebalance_every == 0:
            # Every signal ends yesterday: no current-day close appears in the ranking.
            previous_day, base_day = days[i - 1], days[i - lookback - 1]
            ranks = []
            for symbol, bars in panel.items():
                recent = [bars.get(candidate_day) for candidate_day in days[i - lookback - 1:i]]
                history_count = initial_history + sum(1 for candidate_day in days[:i] if candidate_day in bars)
                if base_day not in bars or previous_day not in bars or day not in bars or history_count < min_history or any(bar is None for bar in recent):
                    continue
                amounts = [bar.amount for bar in recent]
                if min_avg_amount and (any(amount is None for amount in amounts) or sum(amounts) / len(amounts) < min_avg_amount):
                    continue
                ranks.append((bars[previous_day].close / bars[base_day].close - 1, symbol))
            targets = {symbol for _, symbol in sorted(ranks, reverse=True)[:top_n]}
            rebalances += 1
            # 重新入选则取消待退出：排名虽旧但仍然有效。
            pending_exits -= targets

            # Sell dropped names first, so their released cash may fund target purchases.
            for symbol, shares in list(holdings.items()):
                if symbol in targets:
                    continue
                if day not in panel[symbol]:
                    # 停牌：当日根本无法卖出，挂起到复牌，而不是当作没发生。
                    if symbol not in pending_exits:
                        pending_exits.add(symbol)
                        suspension_blocked_sells += 1
                    continue
                bar = panel[symbol][day]
                reference = reference_close.get(symbol)
                if reference is not None and bar.open / reference - 1 <= -limit_ratio(symbol) + 0.001:
                    blocked_sells += 1
                    continue
                value = shares * bar.open * (1 - slip)
                cash += value - _fee(value, commission_rate, minimum_commission) - value * stamp_duty_rate
                del holdings[symbol]
                pending_exits.discard(symbol)
                trades += 1

            candidates = [symbol for symbol in targets if symbol not in holdings]
            # Equal allocations for new names; retained positions are deliberately not churned.
            allocation = cash / len(candidates) if candidates else 0
            for symbol in sorted(candidates):
                bar, previous_close = panel[symbol][day], panel[symbol][previous_day].close
                if bar.open / previous_close - 1 >= limit_ratio(symbol) - 0.001:
                    blocked_buys += 1
                    continue
                price = bar.open * (1 + slip)
                shares = int((allocation - minimum_commission) / (price * (1 + commission_rate)) / 100) * 100
                if shares < 100:
                    continue
                value = shares * price
                cost = value + _fee(value, commission_rate, minimum_commission)
                if cost <= cash:
                    cash -= cost
                    holdings[symbol] = shares
                    trades += 1

        # A suspended stock has no daily row; mark it at its last available close,
        # rather than treating its value as zero on that calendar date.  This is also
        # why the curve goes flat during a suspension: it looks like low volatility
        # but it actually means the position cannot be sold.  suspension_days counts
        # those days so the understatement is visible instead of silent.
        suspension_days += sum(1 for symbol in holdings if day not in panel[symbol])
        value = cash + sum(shares * last_close[symbol] for symbol, shares in holdings.items())
        curve.append(value)

    final_value = curve[-1]
    high_water, max_drawdown = curve[0], 0.0
    for value in curve:
        high_water = max(high_water, value)
        max_drawdown = min(max_drawdown, value / high_water - 1)
    years = len(days) / 252
    total_return = final_value / initial_cash - 1
    annualized = (final_value / initial_cash) ** (1 / years) - 1 if years else None
    return RotationResult(symbols, days[0], days[-1], initial_cash, final_value, total_return, annualized,
                          max_drawdown, trades, rebalances, blocked_buys, blocked_sells,
                          suspension_days, suspension_blocked_sells)


def walk_forward_rotation(panel: dict[str, dict[str, Bar]], train_ratio: float = 0.7, **kwargs: object) -> WalkForwardResult:
    """Run independent in-sample and holdout tests; no positions cross the split."""
    if not 0.5 <= train_ratio < 0.9:
        raise ValueError("train_ratio 必须在 0.5 至 0.9 之间")
    lookback = int(kwargs.get("lookback", 20))
    days = sorted({day for bars in panel.values() for day in bars})
    split = int(len(days) * train_ratio)
    if split <= lookback + 1 or len(days) - split <= lookback + 1:
        raise ValueError("训练段和验证段都必须至少包含 lookback + 2 个交易日")
    train_days, validation_days = set(days[:split]), set(days[split:])
    train_panel = {symbol: {day: bar for day, bar in bars.items() if day in train_days} for symbol, bars in panel.items()}
    validation_panel = {symbol: {day: bar for day, bar in bars.items() if day in validation_days} for symbol, bars in panel.items()}
    validation_kwargs = {**kwargs, "initial_history": split}
    return WalkForwardResult(days[split], momentum_rotation_backtest(train_panel, **kwargs), momentum_rotation_backtest(validation_panel, **validation_kwargs))
