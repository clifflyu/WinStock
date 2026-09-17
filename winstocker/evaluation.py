"""Market-baseline comparisons for strategy research."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .backtest import Bar


@dataclass(frozen=True)
class BenchmarkComparison:
    benchmark: str
    start: str
    end: str
    benchmark_return: float
    strategy_return: float
    excess_return: float


def compare_buy_and_hold(benchmark: str, bars: Iterable[Bar], strategy_start: str, strategy_end: str, strategy_return: float) -> BenchmarkComparison:
    bars = sorted((bar for bar in bars if strategy_start <= bar.day <= strategy_end), key=lambda bar: bar.day)
    if len(bars) < 2:
        raise ValueError("基准数据与策略区间重叠不足，无法比较")
    start_gap = (date.fromisoformat(bars[0].day) - date.fromisoformat(strategy_start)).days
    end_gap = (date.fromisoformat(strategy_end) - date.fromisoformat(bars[-1].day)).days
    if start_gap > 7 or end_gap > 7:
        raise ValueError(f"基准未覆盖完整策略区间（基准 {bars[0].day} 至 {bars[-1].day}），拒绝生成比较结论")
    market_return = bars[-1].close / bars[0].close - 1
    return BenchmarkComparison(benchmark, bars[0].day, bars[-1].day, market_return, strategy_return, strategy_return - market_return)
