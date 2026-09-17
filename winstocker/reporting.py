"""Persist reproducible backtest records instead of relying on terminal output."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


DISCLAIMERS = [
    "研究回测不是收益承诺，也不能直接用于实盘下单。",
    "当前数据不含历史 ST 标记、完整停复牌状态、现金分红和实时盘口成交。",
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
