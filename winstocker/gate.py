"""Conservative research gate: passing permits paper observation, never live trading."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchGate:
    passed: bool
    reasons: tuple[str, ...]


def evaluate_gate(audit: object, validation: object, comparison: object, max_drawdown: float = -0.20, min_trades: int = 10, robustness: object | None = None) -> ResearchGate:
    reasons: list[str] = []
    # 这里必须与 audit 的结论一致：此前只看 integrity/failures，导致 audit 打印
    # 「需检查」的同时 check 仍可能放行。未上市与停牌都不算缺陷，故只看真实失败项。
    if audit.integrity != "ok" or audit.failures:
        reasons.append("数据完整性或下载任务异常。")
    if audit.no_data_failed:
        reasons.append(f"{audit.no_data_failed} 只证券抓取失败且完全没有日线。")
    if audit.lagging_failed:
        reasons.append(f"{audit.lagging_failed} 只证券数据落后且并非停牌所致。")
    if audit.broken_change_rows:
        reasons.append(f"前复权序列有 {audit.broken_change_rows} 行尺度异常，回测收益率不可信。")
    holdout = validation.validation
    if holdout.trades < min_trades:
        reasons.append(f"样本外仅 {holdout.trades} 笔成交，低于 {min_trades} 笔最低样本门槛。")
    if holdout.total_return <= 0:
        reasons.append("样本外收益非正。")
    if holdout.max_drawdown <= max_drawdown:
        reasons.append(f"样本外最大回撤 {holdout.max_drawdown:.2%}，超过 {abs(max_drawdown):.0%} 警戒线。")
    if comparison.excess_return <= 0:
        reasons.append("全期间未跑赢市场基准。")
    if robustness and not robustness.passed:
        reasons.extend(robustness.reasons)
    if not reasons:
        reasons.append("仅允许进入模拟观察，不代表可以实盘。")
    return ResearchGate(len(reasons) == 1 and reasons[0].startswith("仅允许"), tuple(reasons))
