# WinStock research workflow

Run commands from the repository root. The default database is `data/winstock.db`; add `--db PATH` when the user has configured another database.

## Start with data fitness

```bash
python -m winstocker status
python -m winstocker audit
```

`audit` reports SQLite integrity, active securities, symbols with bars, total rows, the latest **complete coverage** day, newest observed day, symbols with no bars (split into fetch failures vs not-yet-listed), lagging symbols (split into suspended vs genuinely stale), suspended symbols, forward-adjustment self-consistency, and pending download failures. Not-yet-listed and suspended symbols are normal market states and do not fail the audit; only real fetch failures, non-suspension lag, and scale drift do. Do not interpret broad cross-sectional results when it says `需检查`.

The systemd timer ordinarily runs daily at 19:30 Asia/Shanghai. It invokes a single `daily` command that chains update → audit → candidates → Feishu push. `update` re-fetches from **each stock's own last stored day** (not one global date, which would leave holes for stocks that missed days), so it corrects source changes without downloading the full date range again. It still queries roughly 5,200 stocks. Before fetching it runs a network-free forward-adjustment self-check and refetches any drifted stock from its own first trading day. A successful update saves a dated candidate snapshot only when there are zero download failures, then rebuilds the trading calendar and runs `audit`.

`daily` reports every step's failure surface rather than aborting on the first error: **the Feishu card is sent even when the update fails**, because silence is indistinguishable from "nothing happened today". The full audit verdict goes to the journal as well as the card. Exit codes: `0` all good, `1` update failed but the red card was delivered, `2` data fine but the card failed to send, `3` both failed. `--no-update` skips fetching and pushes from the existing database. `--dry-run` prints the card without sending and needs no credentials.

Because a red or orange card is itself the alarm, `daily` never raises before attempting delivery — a failure to open the database degrades to a red card carrying the reason, not a traceback.

## Candidate review

```bash
python -m winstocker candidates --top-n 10 --output reports/candidates.json
python -m winstocker snapshot-status
```

Default candidates use the latest complete coverage day, at least 250 history days, at least 20 million yuan average turnover over the 20-day lookback, a contiguous momentum window against the trading calendar, and a name-based ST exclusion. The contiguity check is real, not nominal: without it a suspension inside the window makes the "20-day momentum" span 25+ market days. The JSON contains `symbol`, `name`, `as_of`, `momentum`, `average_amount`, and `history_days`.

Snapshots preserve the pool known on each date. Prefer accumulating and using them for future universe research instead of applying the current list of listed companies backward in time.

The daily Feishu card's Top 10 uses exactly the same parameters as `save_default_candidate_snapshot` (top_n=10, lookback=20, min_history=250, min_avg_amount=2e7), so what the card shows matches what was archived for that date. Overriding `daily --top-n` changes only the card, never the snapshot — this keeps the archived research universe from drifting with presentation settings.

## Push-chain self-check

```bash
python -m winstocker notify --test        # 配置自检卡片，秒级，不读数据库
python -m winstocker notify               # 用当前库内容发一张真实播报卡片
python -m winstocker daily --no-update --dry-run   # 只打印卡片 JSON，不发送
```

The webhook resolves from `--webhook` first, then `WINSTOCK_FEISHU_WEBHOOK`; the signing secret from `--secret`, then `WINSTOCK_FEISHU_SECRET`. Deployment lives in `deploy/飞书推送部署指南.md`; the credential file is `/etc/winstock/notify.env` (0600, outside the repository) because the systemd units install world-readable.

## Strategy research

```bash
# One stock: prior-close signal, next-open execution
python -m winstocker backtest 600000 --fast 20 --slow 60 --output reports/600000-ma.json

# Explicit, historically defensible universe only
python -m winstocker rotation --symbols 600000,000001,300750,600519 --top-n 3 --output reports/rotation.json
python -m winstocker validate --symbols 600000,000001,300750,600519 --output reports/validation.json
python -m winstocker compare --symbols 600000,000001,300750,600519 --benchmark 000300 --output reports/compare.json
python -m winstocker check --symbols 600000,000001,300750,600519 --output reports/gate.json
```

Defaults: 100,000 initial cash; 0.03% commission with a 5-yuan minimum; 0.05% sell-side stamp duty; 5 bp one-side slippage; board-lot orders. Backtests prevent buys at conventional opening limit-up and sells at opening limit-down. ChiNext/STAR are modeled with 20% limits; other covered boards at 10%.

The momentum window is shared with `candidates` on purpose: both measure `P(t−1)/P(t−1−lookback)`, i.e. `lookback` trading-day intervals ending yesterday. If these ever diverge, the live candidate list is no longer the strategy that was validated. A backtest therefore needs at least `lookback + 2` days.

Suspensions are now modeled: results carry `suspension_days` (days a holding was suspended) and `suspension_blocked_sells` (exits deferred to resumption). A position that should be sold while suspended is queued and sold at the open on the first day it trades again, with the limit check using the last close *before* the suspension rather than a same-day close. This matters because a suspended holding is marked at its last close, flattening the equity curve — it reads as low volatility when it actually means the position cannot be sold, so drawdown and volatility are understated without these fields.

Known omissions now include new-share initial no-limit days, historical ST state, half-day suspensions, cash dividends, and order-book fillability. Suspension state is derived from daily-bar gaps, so it sees whole-day suspensions only and depends on that symbol's last fetch having succeeded. Daily bars cannot justify intraday execution claims.

`rotation`, `validate`, and `compare` require a user-provided stock universe. Never substitute all currently active securities for an old historical universe: delistings and future entrants create survivorship bias.

`validate` isolates the latter period chronologically. `compare` uses price-index buy-and-hold (default CSI 300, `000300`) as a basic benchmark, not a total-return index. `check` examines 60/40, 70/30, and 80/20 splits. Its pass conditions are positive out-of-sample results in every split, drawdown no worse than 20%, at least 10 out-of-sample trades, audit health, and benchmark outperformance. A pass means only `允许模拟观察`.

## Paper monitoring

```bash
python -m winstocker paper-init --name research --cash 100000
python -m winstocker paper-status --name research
python -m winstocker paper-mark --name research
```

This is local bookkeeping only. It does not connect to a broker or construct trading orders.
