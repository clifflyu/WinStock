"""Multiple time-split checks to reduce reliance on one lucky validation window."""
from __future__ import annotations

from dataclasses import dataclass

from .backtest import WalkForwardResult, walk_forward_rotation


@dataclass(frozen=True)
class RobustnessResult:
    results: tuple[WalkForwardResult, ...]
    passed: bool
    reasons: tuple[str, ...]


def walk_forward_robustness(panel: object, ratios: tuple[float, ...] = (0.6, 0.7, 0.8), max_drawdown: float = -0.20, min_trades: int = 10, **kwargs: object) -> RobustnessResult:
    results = tuple(walk_forward_rotation(panel, ratio, **kwargs) for ratio in ratios)
    reasons: list[str] = []
    for ratio, result in zip(ratios, results):
        holdout = result.validation
        if holdout.trades < min_trades:
            reasons.append(f"{ratio:.0%} 切分的样本外成交不足 {min_trades} 笔。")
        if holdout.total_return <= 0:
            reasons.append(f"{ratio:.0%} 切分的样本外收益非正。")
        if holdout.max_drawdown <= max_drawdown:
            reasons.append(f"{ratio:.0%} 切分的样本外回撤 {holdout.max_drawdown:.2%} 超过警戒线。")
    return RobustnessResult(results, not reasons, tuple(reasons))
