"""Persist reproducible backtest records instead of relying on terminal output."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


DISCLAIMERS = [
    "研究回测不是收益承诺，也不能直接用于实盘下单。",
    "停牌状态由日线缺口推导，只能识别整天停牌，且依赖该股最近一次抓取成功；"
    "数据仍不含历史 ST 标记、现金分红和实时盘口成交。",
    "股票池须按历史时点固化；用当前全市场名单回测会产生幸存者偏差。",
]


def research_warnings(result: Any) -> list[str]:
    """Simple gates that flag insufficient evidence; they never certify a strategy."""
    warnings = list(DISCLAIMERS)
    if result.trades < 10:
        warnings.append(f"仅 {result.trades} 笔成交，样本过少，不能据此判断策略有效。")
    if result.max_drawdown <= -0.20:
        warnings.append(f"最大回撤 {result.max_drawdown:.2%}，超过 20% 研究警戒线。")
    if result.total_return <= 0:
        warnings.append("区间总收益非正，不应进入模拟交易阶段。")
    if result.blocked_sells:
        warnings.append(f"发生 {result.blocked_sells} 次跌停无法卖出；实盘流动性风险可能更高。")
    if getattr(result, "suspension_days", 0):
        warnings.append(
            f"持仓有 {result.suspension_days} 天处于停牌：这段净值按停牌前收盘价挂着，"
            f"曲线会走平，看起来像低波动，实际是无法卖出。回撤与波动率因此被低估。"
        )
    if getattr(result, "suspension_blocked_sells", 0):
        warnings.append(
            f"有 {result.suspension_blocked_sells} 次调仓因停牌无法卖出，已推迟到复牌当日成交。"
        )
    return warnings


def write_backtest_report(path: Path, strategy: str, parameters: dict[str, Any], result: Any) -> Path:
    if not is_dataclass(result):
        raise TypeError("result 必须是 dataclass")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "strategy": strategy,
        "parameters": parameters,
        "result": asdict(result),
        "warnings": research_warnings(result),
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_walk_forward_report(path: Path, parameters: dict[str, Any], result: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    warnings = research_warnings(result.train) + research_warnings(result.validation)
    if result.validation.total_return <= 0:
        warnings.append("样本外验证收益非正：策略未通过进入模拟交易的最低研究门槛。")
    if result.validation.annualized_return is not None and result.train.annualized_return is not None and result.validation.annualized_return < result.train.annualized_return * 0.4:
        warnings.append("样本外年化显著弱于训练段，存在过拟合或市场状态变化风险。")
    document = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "strategy": "momentum_rotation_walk_forward", "parameters": parameters, "result": asdict(result), "warnings": warnings}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_comparison_report(path: Path, parameters: dict[str, Any], strategy_result: Any, comparison: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    warnings = research_warnings(strategy_result)
    if comparison.excess_return <= 0:
        warnings.append("策略未跑赢基准买入持有，不应作为主动交易候选。")
    document = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "strategy": "momentum_rotation_comparison", "parameters": parameters, "strategy_result": asdict(strategy_result), "benchmark_comparison": asdict(comparison), "warnings": warnings}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
