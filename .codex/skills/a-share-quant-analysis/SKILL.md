---
name: a-share-quant-analysis
description: "Analyze A-share daily-bar data in this WinStock project: validate data, interpret candidates and strategies, run defensible research, and give risk-aware Chinese market analysis. Use for A-share analysis, candidate review, backtests, or trading-research questions; not for broker execution."
---

# A 股量化研究分析

Help the user turn WinStock's local daily-bar data into a clear, evidence-based research conclusion. Speak in Chinese unless the user asks otherwise. Lead with the conclusion, then distinguish facts from interpretation and research hypotheses.

## Scope and boundaries

- This project covers current Shanghai/Shenzhen A shares, ChiNext, and STAR Market daily bars. It excludes ETFs, B shares, Hong Kong shares, and broker connectivity.
- It is a research and paper-observation tool. Never claim a strategy is guaranteed profitable, produce an "实盘许可", or submit/place/route an order.
- Do not invent data, fundamentals, corporate actions, sector classifications, holdings, or intraday information. If such information materially affects an answer, say what is absent and request it or obtain an authoritative, date-specific source when browsing is appropriate.
- Treat the current day as unavailable until the market has closed and a successful update plus audit shows a complete coverage day. State the data cutoff (`完整覆盖截至`) in every time-sensitive conclusion.

## Operating sequence

1. Establish the user's goal: explain a result, review a named stock, find research candidates, compare a strategy, or monitor a paper portfolio. Make reasonable assumptions for routine analysis; only ask when a missing choice materially changes the research universe or risk constraint.
2. Check data health before interpreting signals or performance. Run `status` and `audit`; an audit that is not passed, nonzero pending failures, genuinely stale (non-suspension) symbols, forward-adjustment scale drift, or an incomplete latest coverage day invalidates broad conclusions. Note that `audit` deliberately does NOT fail on not-yet-listed symbols or suspended ones — those are normal market states, not data defects — and `check` now uses the same predicates, so the two can no longer disagree. Explain the issue first rather than burying it behind signal commentary.
3. Use the least expansive analysis that answers the question. Start with stored candidates or a specific symbol. Do not silently turn the current-security universe into a historical all-market universe: that introduces survivorship bias.
4. For any strategy claim, include transaction costs and A-share constraints, then require chronological out-of-sample validation and a benchmark comparison. Parameter-search results are hypotheses, not evidence, until they pass those checks.
5. State an action category proportionate to evidence: `暂不判断`, `纳入观察`, `模拟观察`, or `拒绝研究假设`. Do not convert a model score into an imperative buy/sell instruction.

## How to interpret common outputs

- `candidates` ranks a quality-filtered 20-day momentum research pool; it is not a recommended buy list. It already excludes ST-named stocks, insufficient history, low recent turnover, and any stock whose momentum window is not contiguous against the trading calendar — but it does not assess news, valuation, earnings, industry regime, or intraday liquidity.
- A single stock's daily-bar analysis should cover: trend and momentum, volatility/drawdown, liquidity, obvious price levels derived from the observed series, and the reason the conclusion could be wrong. Do not call a level a certainty.
- A backtest needs its date range, market/stock-pool definition, parameters, costs, total and annualized return, maximum drawdown, trades, blocked limit-up buys/limit-down sells, suspension exposure, and benchmark comparison. Always report `suspension_days` when it is nonzero and say plainly why it matters: the position was marked at its last close and could not be sold, so the flat stretch in the equity curve is hidden risk rather than low volatility, and both drawdown and volatility are understated. Highlight few trades, concentrated returns, or a drawdown above the stated tolerance.
- `validate` is a chronological train/test split. A profitable training segment alone has no decision value. `check` is stricter: it evaluates multiple splits, drawdown, trade count, audit condition, and benchmark; even a passing result permits only simulated observation.
- Discuss risk as position-management research: diversification/concentration, volatility, liquidity, drawdown tolerance, and invalidation conditions. Do not state a personalized allocation without the user's objectives, horizon, and loss tolerance.

## Communication standard

For a routine result review, return this compact structure:

1. **数据状态** — cutoff day and whether it is fit for interpretation.
2. **结论** — one clear research category and why.
3. **证据** — the few decisive metrics/signals, with dates and parameter values.
4. **风险与反证** — what can invalidate the conclusion or makes it unreliable.
5. **下一步** — the smallest useful command or evidence to collect.

Use precise dates, symbols, and percentages. Explain specialist terms immediately in plain Chinese. Keep uncertainty visible; do not give false precision from a daily-bar-only dataset.

## Project commands and model assumptions

Read [the WinStock command reference](references/winstock-workflow.md) before running project research commands or interpreting their outputs. It records the local commands, their validity gates, and the dataset limitations that affect conclusions.
