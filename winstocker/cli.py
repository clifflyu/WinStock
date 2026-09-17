from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import math
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .audit import DataAudit, audit_database, scale_drift
from .backtest import Bar, dual_ma_backtest, load_bars, load_panel, momentum_rotation_backtest, walk_forward_rotation
from .calendar import ensure_calendar, refresh_calendar, trading_days_between
from .candidates import Candidate, momentum_candidates
from .evaluation import compare_buy_and_hold
from .enrichment import enrichment_status, update_financials, update_industries
from .gate import evaluate_gate
from .intraday import run_execution_experiment
from .notify import (ERROR_MAX_CHARS, SECRET_ENV, WEBHOOK_ENV, DailyDigest, NotifyTarget, PreviousPoolBacktest,
                     build_card, build_morning_card, build_test_card, credential, require_credential, send_card)
from .paper import (PaperRebalance, account_status, create_account, enable_auto_strategy, mark_account,
                    rebalance_account)
from .paper_flow import close_enabled_accounts, create_evening_plans, execute_pending_plans
from .reporting import write_backtest_report, write_comparison_report, write_walk_forward_report
from .robustness import walk_forward_robustness
from .snapshots import DEFAULT_STRATEGY_KEY, previous_snapshot, save_snapshot, snapshot_status

LOG = logging.getLogger("winstocker")
# 清单取自东方财富的延时行情主机：push2 与 push2his 会对海外和机房 IP 直接断连。
LIST_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
# 日 K 改用腾讯：东方财富的 push2his 在同样的 IP 上不可达。
KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
KLINE_PAGE_SIZE = 640  # 腾讯单次返回上限，区间更长时需从 end 反向翻页
KLINE_MAX_PAGES = 40  # 翻页安全上限，正常区间远用不到
EX_DIV_TOLERANCE = 0.01  # 复权因子相对变化超过此值即认定当天除权
SHARES_BOARD = ("688", "689")  # 科创板代码前缀，其成交量腾讯按股返回
DEFAULT_DB = Path("data/winstock.db")
# 北京时间固定 UTC+8、无夏令时，用固定偏移即可；zoneinfo 在精简容器里可能缺 tzdata。
BEIJING = timezone(timedelta(hours=8))
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 数值列以定点整数存储（乘以缩放系数后取整），体积约为 REAL 的一半；
# daily_kline 视图负责还原成小数，查询写法与改版前一致。
KLINE_COLUMNS = ["open", "close", "high", "low", "volume", "amount", "amplitude", "change_pct", "change_amount", "turnover"]
VALUE_SCALES = {
    "open": 1000, "close": 1000, "high": 1000, "low": 1000,
    "volume": 100, "amount": 100,
    "amplitude": 1000, "change_pct": 1000, "change_amount": 1000, "turnover": 1000,
}
KLINE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS securities (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL UNIQUE,
    market INTEGER NOT NULL,
    secid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    exchange TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    listed_at TEXT,
    updated_at TEXT NOT NULL
);
-- WITHOUT ROWID 让主键不再是单独的索引树，省掉一份 key 的重复存储。
CREATE TABLE IF NOT EXISTS kline (
    symbol_id INTEGER NOT NULL REFERENCES securities(id),
    trade_date TEXT NOT NULL,
    {", ".join(f"{name} INTEGER" for name in KLINE_COLUMNS)},
    PRIMARY KEY (symbol_id, trade_date)
) WITHOUT ROWID;
CREATE VIEW IF NOT EXISTS daily_kline AS
    SELECT s.symbol AS symbol, k.trade_date AS trade_date,
           {", ".join(f"k.{name} / {VALUE_SCALES[name]}.0 AS {name}" for name in KLINE_COLUMNS)}
    FROM kline k JOIN securities s ON s.id = k.symbol_id;
CREATE TABLE IF NOT EXISTS download_failures (
    symbol TEXT PRIMARY KEY,
    error TEXT NOT NULL,
    failed_at TEXT NOT NULL
);
"""


def request_json(url: str, params: dict[str, Any], retries: int = 3, referer: str | None = None) -> dict[str, Any]:
    """Fetch JSON with bounded retries; no third-party HTTP dependency required."""
    full_url = f"{url}?{urlencode(params)}"
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(full_url, headers=headers)
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("data") is None:
                raise RuntimeError(f"行情接口未返回 data: {payload.get('msg', 'unknown error')}")
            return payload
        except Exception as error:  # preserve the actual endpoint error for the caller
            last_error = error
            if attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"请求失败: {last_error}") from last_error


def migrate_legacy(conn: sqlite3.Connection) -> None:
    """把旧布局（TEXT 主键 + REAL 数值）的库就地转换成新布局，免去重新下载。"""
    found = conn.execute("SELECT type FROM sqlite_master WHERE name = 'daily_kline'").fetchone()
    if not found or found[0] != "table":
        return
    LOG.info("检测到旧版表结构，正在就地转换（大库可能需要一两分钟）…")
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_daily_kline_trade_date;
        ALTER TABLE daily_kline RENAME TO daily_kline_legacy;
        ALTER TABLE securities RENAME TO securities_legacy;
        """
    )
    conn.executescript(KLINE_SCHEMA)
    with conn:
        conn.execute(
            """INSERT INTO securities(symbol, market, secid, name, exchange, is_active, listed_at, updated_at)
               SELECT symbol, market, secid, name, exchange, is_active, listed_at, updated_at FROM securities_legacy"""
        )
        values = ", ".join(f"CAST(ROUND(l.{name} * {VALUE_SCALES[name]}) AS INTEGER)" for name in KLINE_COLUMNS)
        conn.execute(
            f"""INSERT INTO kline(symbol_id, trade_date, {", ".join(KLINE_COLUMNS)})
                SELECT s.id, l.trade_date, {values}
                FROM daily_kline_legacy l JOIN securities s ON s.symbol = l.symbol"""
        )
        conn.execute("DROP TABLE daily_kline_legacy")
        conn.execute("DROP TABLE securities_legacy")
    conn.execute("VACUUM")
    LOG.info("转换完成。")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # 默认 busy_timeout 为 0：定时任务与手动 init/update 撞上时 sqlite 会立刻抛
    # "database is locked"。等 30 秒足够让另一个写事务结束，消除一整类偶发失败。
    conn.execute("PRAGMA busy_timeout=30000")
    migrate_legacy(conn)
    conn.executescript(KLINE_SCHEMA)
    # 日历与停牌表在升级后的第一次运行时惰性建立；此后开销仅为一次 COUNT。
    ensure_calendar(conn)
    return conn


def security_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return dict(conn.execute("SELECT symbol, id FROM securities"))


def fetch_securities() -> list[dict[str, Any]]:
    # Market/type filters deliberately include only mainland A-share board types.
    # Eastmoney caps this endpoint at 100 records per page, even if pz is larger.
    base_params = {
        # 必须按代码(f12)分页：按涨跌幅(f3)排序时大量个股并列，翻页顺序不稳定，
        # 会在页边界重复和漏掉个股（实测每次运行少 250 只左右）。
        "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
        "fs": "m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2",
        "fields": "f12,f13,f14,f26",
    }
    first_page = request_json(LIST_URL, {**base_params, "pn": 1}, referer="https://quote.eastmoney.com/")["data"]
    rows = list(first_page.get("diff", []))
    total = int(first_page.get("total", len(rows)))
    for page in range(2, math.ceil(total / base_params["pz"]) + 1):
        # Avoid burst traffic while walking the many small pages this API allows.
        time.sleep(0.15)
        rows.extend(request_json(LIST_URL, {**base_params, "pn": page}, referer="https://quote.eastmoney.com/")["data"].get("diff", []))
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol, market, name = str(row.get("f12", "")), row.get("f13"), str(row.get("f14", ""))
        if len(symbol) == 6 and market in (0, 1) and name:
            unique[symbol] = {"symbol": symbol, "market": market, "name": name, "listed_at": row.get("f26")}
    return list(unique.values())


def save_securities(conn: sqlite3.Connection, securities: list[dict[str, Any]]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE securities SET is_active = 0")
    conn.executemany(
        """INSERT INTO securities(symbol, market, secid, name, exchange, is_active, listed_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET market=excluded.market, secid=excluded.secid,
             name=excluded.name, exchange=excluded.exchange, is_active=1,
             listed_at=excluded.listed_at, updated_at=excluded.updated_at""",
        [(s["symbol"], s["market"], f'{s["market"]}.{s["symbol"]}', s["name"], "SZ" if s["market"] == 0 else "SH", s["listed_at"], now) for s in securities],
    )
    conn.commit()


def as_number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def tencent_code(security: dict[str, Any]) -> str:
    # 东方财富用 0/1 区分深沪，腾讯用 sz/sh 前缀。
    return f'{"sz" if security["market"] == 0 else "sh"}{security["symbol"]}'


def fetch_kline_bars(code: str, start: str, end: str, fq: str) -> dict[str, list[Any]]:
    """腾讯接口忽略 start、单次最多返回 KLINE_PAGE_SIZE 根，因此从 end 逐段往回翻。"""
    bars: dict[str, list[Any]] = {}
    cursor = end
    for _ in range(KLINE_MAX_PAGES):
        payload = request_json(KLINE_URL, {"param": f"{code},day,{start},{cursor},{KLINE_PAGE_SIZE},{fq}"})["data"]
        node = payload.get(code) or {}
        page = [row for row in (node.get(f"{fq}day") or node.get("day") or []) if isinstance(row, list) and len(row) >= 9]
        if not page:
            break
        for row in page:
            bars[row[0]] = row
        earliest = min(row[0] for row in page)
        if earliest <= start:
            break
        cursor = (date.fromisoformat(earliest) - timedelta(days=1)).isoformat()
    return bars


def to_lots(symbol: str, volume: float | None) -> float | None:
    """腾讯对科创板返回的成交量单位是股，其余板块是手，统一折算成手。

    已用新浪逐股核对：主板与创业板腾讯值 = 新浪值 / 100，科创板两者完全相等。
    """
    if volume is None or not symbol.startswith(SHARES_BOARD):
        return volume
    return volume / 100


def build_kline_rows(symbol: str, adjusted: dict[str, list[Any]], raw: dict[str, list[Any]]) -> list[tuple[Any, ...]]:
    """把腾讯的 10 字段行转成 daily_kline 的列。

    OHLC 存前复权值；涨跌幅与振幅优先用不复权序列计算——前复权价被压缩且只保留两位
    小数，直接对它求比值会放大舍入误差（实测最大 0.49 个百分点，约四分之一交易日偏差
    超过 0.1）。只有复权因子在除权日发生跳变时才回退到前复权比值，否则跨不过除权缺口。

    返回值按 VALUE_SCALES 缩放成整数，与 kline 表的列类型一致。
    """
    rows: list[tuple[Any, ...]] = []
    previous: dict[str, float | None] = {"adjusted": None, "raw": None, "factor": None}
    for day in sorted(adjusted):
        row = adjusted[day]
        open_, close, high, low = (as_number(row[index]) for index in range(1, 5))
        raw_row = raw.get(day) or []
        raw_close = as_number(raw_row[2]) if len(raw_row) > 2 else None
        amount = as_number(row[8])
        if amount is not None:
            amount *= 10000  # 腾讯以万元计，与东方财富的元对齐

        factor = close / raw_close if close is not None and raw_close else None
        ex_dividend = bool(
            factor and previous["factor"] and abs(factor / previous["factor"] - 1) > EX_DIV_TOLERANCE
        )

        change_amount = change_pct = amplitude = None
        if previous["adjusted"] and close is not None:
            change_amount = close - previous["adjusted"]
            change_pct = (close - previous["adjusted"]) / previous["adjusted"] * 100
            if high is not None and low is not None:
                amplitude = (high - low) / previous["adjusted"] * 100
            if not ex_dividend and previous["raw"] and raw_close is not None:
                change_pct = (raw_close - previous["raw"]) / previous["raw"] * 100
                raw_high, raw_low = as_number(raw_row[3]), as_number(raw_row[4])
                if raw_high is not None and raw_low is not None:
                    amplitude = (raw_high - raw_low) / previous["raw"] * 100

        if close is not None:
            previous["adjusted"] = close
        if raw_close is not None:
            previous["raw"] = raw_close
        if factor is not None:
            previous["factor"] = factor
        values = (open_, close, high, low, to_lots(symbol, as_number(row[5])), amount, amplitude, change_pct, change_amount, as_number(row[7]))
        rows.append((
            symbol, day,
            *(None if value is None else round(value * VALUE_SCALES[name]) for name, value in zip(KLINE_COLUMNS, values)),
        ))
    return rows


def fetch_kline(security: dict[str, Any], start: str, end: str) -> tuple[str, list[tuple[Any, ...]]]:
    code = tencent_code(security)
    adjusted = fetch_kline_bars(code, start, end, "qfq")
    raw = fetch_kline_bars(code, start, end, "")
    # 翻页会带出 start 之前的若干根，它们只用来给首根提供前收盘，不入库。
    rows = [row for row in build_kline_rows(security["symbol"], adjusted, raw) if start <= row[1] <= end]
    return security["symbol"], rows


def batches(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for offset in range(0, len(items), size):
        yield items[offset: offset + size]


def run_init(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        LOG.info("正在获取 A 股清单…")
        securities = fetch_securities()
        if not securities:
            raise RuntimeError("A 股清单为空，终止下载。")
        save_securities(conn, securities)
        ids = security_ids(conn)
        LOG.info("已保存 %d 只 A 股，开始下载 %s 至 %s 的日 K。", len(securities), args.start, args.end)
        successful, failed = 0, 0
        for number, group in enumerate(batches(securities, args.batch_size), start=1):
            rows_to_save: list[tuple[Any, ...]] = []
            errors: list[tuple[str, str, str]] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(fetch_kline, item, args.start, args.end): item for item in group}
                for future in concurrent.futures.as_completed(futures):
                    security = futures[future]
                    symbol = security["symbol"]
                    try:
                        _, rows = future.result()
                        rows_to_save.extend(rows)
                        successful += 1
                        # 条数为 0 说明接口成功返回但无数据（停牌或次新股），并非真正的同步成功。
                        LOG.info("%s %s：写入 %d 条日 K", symbol, security["name"], len(rows))
                    except Exception as error:
                        failed += 1
                        message = str(error)[:1000]
                        errors.append((symbol, message, datetime.now().isoformat(timespec="seconds")))
                        LOG.warning("%s %s：下载失败（%s）", symbol, security["name"], message)
            with conn:
                conn.executemany(
                    f"""INSERT INTO kline(symbol_id, trade_date, {", ".join(KLINE_COLUMNS)})
                       VALUES ({", ".join("?" * (len(KLINE_COLUMNS) + 2))})
                       ON CONFLICT(symbol_id, trade_date) DO UPDATE SET
                       {", ".join(f"{name}=excluded.{name}" for name in KLINE_COLUMNS)}""",
                    [(ids[row[0]], *row[1:]) for row in rows_to_save],
                )
                conn.executemany("DELETE FROM download_failures WHERE symbol = ?", [(item["symbol"],) for item in group if item["symbol"] not in {e[0] for e in errors}])
                conn.executemany("INSERT INTO download_failures(symbol, error, failed_at) VALUES (?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET error=excluded.error, failed_at=excluded.failed_at", errors)
            LOG.info("批次 %d：完成 %d/%d（成功 %d，失败 %d，写入 %d 条日 K）", number, min(number * args.batch_size, len(securities)), len(securities), successful, failed, len(rows_to_save))
        days, suspended = refresh_calendar(conn)
        LOG.info("已重建交易日历：%d 个交易日，%d 条停牌记录。", days, suspended)
        LOG.info("初始化结束：成功 %d，失败 %d。运行 `python -m winstocker status` 查看详情。", successful, failed)
    finally:
        conn.close()


def symbol_starts(conn: sqlite3.Connection, end: str, bootstrap_start: str, repair: set[str]) -> dict[str, str]:
    """每只股票各自的抓取起点。

    默认从该股自己的最后一个已存交易日开始，而不是全库 MAX：用全库 MAX 时，一只中途
    漏抓或停牌的股票永远补不回它自己的缺口。

    对检测出前复权尺度漂移的股票，起点改用它自己的首个交易日以重抓全部历史——腾讯的
    前复权按「减去累计现金分红」构造，一次除权会让整条历史换尺度，只重写最新几天等于
    没修（这正是 600009 那类假跳变的成因）。
    """
    starts: dict[str, str] = {}
    for symbol, first_day, last_day in conn.execute(
        """SELECT s.symbol, MIN(k.trade_date), MAX(k.trade_date)
           FROM securities s JOIN kline k ON k.symbol_id = s.id GROUP BY s.id"""
    ):
        starts[symbol] = first_day if symbol in repair else min(last_day, end)
    return starts


def save_default_candidate_snapshot(conn: sqlite3.Connection) -> int:
    picks = momentum_candidates(conn, top_n=10, lookback=20, min_history=250, min_avg_amount=20_000_000)
    return save_snapshot(conn, DEFAULT_STRATEGY_KEY, picks)


def run_update(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        LOG.info("正在刷新 A 股清单…")
        securities = fetch_securities()
        if not securities:
            raise RuntimeError("A 股清单为空，终止更新。")
        save_securities(conn, securities)
        latest = conn.execute("SELECT MAX(trade_date) FROM kline").fetchone()[0]
        if latest and latest > args.end:
            raise ValueError(f"数据库已存到 {latest}，晚于 --end {args.end}；请指定更晚的 --end")
        drift = scale_drift(conn)
        repair = {symbol for symbol, _ in drift}
        if repair:
            LOG.warning("检测到 %d 只股票的前复权序列存在尺度漂移（%d 处边界），将从各自首个交易日重抓全史修复：%s",
                        len(repair), len(drift), "、".join(sorted(repair)[:10]) + ("…" if len(repair) > 10 else ""))
        starts = symbol_starts(conn, args.end, args.bootstrap_start, repair)
        ids = security_ids(conn)
        LOG.info("增量更新至 %s，共 %d 只 A 股（其中 %d 只按各自末日增量，%d 只全史修复）。",
                 args.end, len(securities), len(securities) - len(repair), len(repair))
        successful, failed = 0, 0
        for number, group in enumerate(batches(securities, args.batch_size), start=1):
            rows_to_save: list[tuple[Any, ...]] = []
            errors: list[tuple[str, str, str]] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(fetch_kline, item, starts.get(item["symbol"], args.bootstrap_start), args.end): item for item in group}
                for future in concurrent.futures.as_completed(futures):
                    security = futures[future]
                    try:
                        _, rows = future.result()
                        rows_to_save.extend(rows)
                        successful += 1
                    except Exception as error:
                        failed += 1
                        errors.append((security["symbol"], str(error)[:1000], datetime.now().isoformat(timespec="seconds")))
                        LOG.warning("%s %s：更新失败（%s）", security["symbol"], security["name"], errors[-1][1])
            with conn:
                conn.executemany(
                    f"""INSERT INTO kline(symbol_id, trade_date, {", ".join(KLINE_COLUMNS)})
                       VALUES ({", ".join("?" * (len(KLINE_COLUMNS) + 2))})
                       ON CONFLICT(symbol_id, trade_date) DO UPDATE SET
                       {", ".join(f"{name}=excluded.{name}" for name in KLINE_COLUMNS)}""",
                    [(ids[row[0]], *row[1:]) for row in rows_to_save],
                )
                failed_symbols = {error[0] for error in errors}
                conn.executemany("DELETE FROM download_failures WHERE symbol = ?", [(item["symbol"],) for item in group if item["symbol"] not in failed_symbols])
                conn.executemany("INSERT INTO download_failures(symbol, error, failed_at) VALUES (?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET error=excluded.error, failed_at=excluded.failed_at", errors)
            LOG.info("批次 %d：完成 %d/%d（成功 %d，失败 %d，写入 %d 条日 K）", number, min(number * args.batch_size, len(securities)), len(securities), successful, failed, len(rows_to_save))
        if not failed and not args.no_snapshot:
            LOG.info("已自动固化 %d 只候选股的历史快照。", save_default_candidate_snapshot(conn))
        elif failed:
            LOG.warning("本次有下载失败，跳过候选快照，避免固化不完整数据。")
        days, suspended = refresh_calendar(conn)
        LOG.info("已重建交易日历：%d 个交易日，%d 条停牌记录。", days, suspended)
        LOG.info("增量更新结束：成功 %d，失败 %d。请运行 `python -m winstocker audit`。", successful, failed)
    finally:
        conn.close()


def run_list(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        securities = fetch_securities()
        save_securities(conn, securities)
        print(f"已刷新 A 股清单：{len(securities)} 只；数据库：{args.db}")
    finally:
        conn.close()


def run_status(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM securities WHERE is_active = 1").fetchone()[0]
        kline_count, first_day, last_day = conn.execute("SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM kline").fetchone()
        failures = conn.execute("SELECT COUNT(*) FROM download_failures").fetchone()[0]
        print(f"数据库：{args.db}\nA 股清单：{count}\n日 K：{kline_count} 条（{first_day or '-'} 至 {last_day or '-'}）\n待重试失败：{failures}")
    finally:
        conn.close()


def audit_report_text(report: DataAudit, max_lag_days: int) -> str:
    """渲染审计结论。终端与 journal 共用同一份口径，避免两处描述漂移。"""
    verdict = "通过" if report.ok else "需检查（不要直接相信回测结果）"
    unlisted = report.no_data_symbols - report.no_data_failed
    suspended_lag = report.lagging_symbols - report.lagging_failed
    if report.broken_change_rows:
        consistency = (f"异常：{report.broken_change_rows} 行的涨跌额与相邻收盘价不符，"
                       f"疑似前复权尺度漂移（重跑完整 init 或 update 可修复）")
    else:
        consistency = "通过（无尺度漂移）"
    return (
        f"数据审计：{verdict}\n数据库完整性：{report.integrity}\n"
        f"活跃证券：{report.active_securities}\n有日线证券：{report.symbols_with_bars}\n"
        f"日线总数：{report.rows}\n完整覆盖截至：{report.latest_day or '-'}（{report.latest_day_symbols} 只）\n"
        f"最新观测日：{report.newest_observed_day or '-'}（{report.newest_observed_symbols} 只）\n"
        f"无日线证券：{report.no_data_symbols}（抓取失败 {report.no_data_failed}，未上市 {unlisted}）\n"
        f"落后最新日超过 {max_lag_days} 天：{report.lagging_symbols} 只"
        f"（停牌 {suspended_lag}，真实落后 {report.lagging_failed}）\n"
        f"区间内停牌股票：{report.suspended_symbols} 只\n"
        f"前复权序列自洽性：{consistency}\n"
        f"下载失败待重试：{report.failures}"
    )


def run_audit(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        print(audit_report_text(audit_database(conn, args.max_lag_days), args.max_lag_days))
    finally:
        conn.close()


def run_backtest(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        bars = load_bars(conn, args.symbol, args.start, args.end)
        result = dual_ma_backtest(
            bars, args.symbol, args.fast, args.slow, args.cash, args.commission,
            args.min_commission, args.stamp_duty, args.slippage_bps,
            trading_days=trading_days_between(conn, args.start, args.end),
        )
        annualized = "-" if result.annualized_return is None else f"{result.annualized_return:.2%}"
        print(
            f"策略：双均线（{args.fast}/{args.slow}），{result.symbol}\n"
            f"区间：{result.start} 至 {result.end}\n"
            f"初始资金：{result.initial_cash:,.2f}\n最终权益：{result.final_value:,.2f}\n"
            f"总收益：{result.total_return:.2%}\n年化收益：{annualized}\n"
            f"最大回撤：{result.max_drawdown:.2%}\n成交笔数：{result.trades}\n"
            f"涨停未买入：{result.blocked_buys}\n跌停未卖出：{result.blocked_sells}\n"
            f"区间内停牌（无法交易）：{result.suspension_days} 天"
        )
        if args.output:
            path = write_backtest_report(args.output, "dual_ma", {
                "symbol": args.symbol, "fast": args.fast, "slow": args.slow, "cash": args.cash,
                "commission": args.commission, "min_commission": args.min_commission,
                "stamp_duty": args.stamp_duty, "slippage_bps": args.slippage_bps,
            }, result)
            print(f"研究报告：{path}")
    finally:
        conn.close()


def run_rotation(args: argparse.Namespace) -> None:
    symbols = tuple(item.strip() for item in args.symbols.split(",") if item.strip())
    conn = connect(args.db)
    try:
        result = momentum_rotation_backtest(
            load_panel(conn, symbols, args.start, args.end), args.top_n, args.lookback, args.rebalance_every,
            args.cash, args.commission, args.min_commission, args.stamp_duty, args.slippage_bps,
            args.min_history, args.min_avg_amount,
        )
        annualized = "-" if result.annualized_return is None else f"{result.annualized_return:.2%}"
        print(
            f"策略：{args.lookback} 日动量前 {args.top_n}，每 {args.rebalance_every} 日调仓\n"
            f"股票池：{','.join(result.symbols)}\n区间：{result.start} 至 {result.end}\n"
            f"初始资金：{result.initial_cash:,.2f}\n最终权益：{result.final_value:,.2f}\n"
            f"总收益：{result.total_return:.2%}\n年化收益：{annualized}\n最大回撤：{result.max_drawdown:.2%}\n"
            f"调仓次数：{result.rebalances}\n成交笔数：{result.trades}\n"
            f"涨停未买入：{result.blocked_buys}\n跌停未卖出：{result.blocked_sells}\n"
            f"停牌无法卖出：{result.suspension_blocked_sells} 次；持仓处于停牌中：{result.suspension_days} 天"
        )
        if args.output:
            path = write_backtest_report(args.output, "momentum_rotation", {
                "symbols": symbols, "top_n": args.top_n, "lookback": args.lookback,
                "rebalance_every": args.rebalance_every, "cash": args.cash, "commission": args.commission,
                "min_commission": args.min_commission, "stamp_duty": args.stamp_duty,
                "slippage_bps": args.slippage_bps, "min_history": args.min_history, "min_avg_amount": args.min_avg_amount,
            }, result)
            print(f"研究报告：{path}")
    finally:
        conn.close()


def run_validate(args: argparse.Namespace) -> None:
    symbols = tuple(item.strip() for item in args.symbols.split(",") if item.strip())
    conn = connect(args.db)
    try:
        result = walk_forward_rotation(
            load_panel(conn, symbols, args.start, args.end), args.train_ratio, top_n=args.top_n,
            lookback=args.lookback, rebalance_every=args.rebalance_every, initial_cash=args.cash,
            commission_rate=args.commission, minimum_commission=args.min_commission,
            stamp_duty_rate=args.stamp_duty, slippage_bps=args.slippage_bps, min_history=args.min_history,
            min_avg_amount=args.min_avg_amount,
        )
        def summary(label: str, item: Any) -> str:
            annualized = "-" if item.annualized_return is None else f"{item.annualized_return:.2%}"
            return (f"{label}（{item.start} 至 {item.end}）：收益 {item.total_return:.2%}，年化 {annualized}，"
                    f"回撤 {item.max_drawdown:.2%}，成交 {item.trades} 笔，"
                    f"持仓停牌 {item.suspension_days} 天")
        print(f"样本外验证切分日：{result.split_day}\n{summary('训练段', result.train)}\n{summary('验证段', result.validation)}")
        if args.output:
            path = write_walk_forward_report(args.output, {
                "symbols": symbols, "train_ratio": args.train_ratio, "top_n": args.top_n,
                "lookback": args.lookback, "rebalance_every": args.rebalance_every, "cash": args.cash,
                "commission": args.commission, "min_commission": args.min_commission,
                "stamp_duty": args.stamp_duty, "slippage_bps": args.slippage_bps,
                "min_history": args.min_history, "min_avg_amount": args.min_avg_amount,
            }, result)
            print(f"研究报告：{path}")
    finally:
        conn.close()


def index_code(symbol: str) -> str:
    """Tencent market prefixes for common mainland broad-market indexes."""
    symbol = symbol.lower()
    if symbol.startswith(("sh", "sz")):
        return symbol
    return f'{"sz" if symbol.startswith("399") else "sh"}{symbol}'


def run_compare(args: argparse.Namespace) -> None:
    symbols = tuple(item.strip() for item in args.symbols.split(",") if item.strip())
    conn = connect(args.db)
    try:
        result = momentum_rotation_backtest(
            load_panel(conn, symbols, args.start, args.end), args.top_n, args.lookback, args.rebalance_every,
            args.cash, args.commission, args.min_commission, args.stamp_duty, args.slippage_bps,
            args.min_history, args.min_avg_amount,
        )
        raw = fetch_kline_bars(index_code(args.benchmark), result.start, result.end, "qfq")
        bars = [Bar(day, float(row[1]), float(row[2])) for day, row in raw.items() if len(row) > 2]
        comparison = compare_buy_and_hold(args.benchmark, bars, result.start, result.end, result.total_return)
        print(
            f"策略收益：{comparison.strategy_return:.2%}\n{args.benchmark} 买入持有：{comparison.benchmark_return:.2%}\n"
            f"超额收益：{comparison.excess_return:.2%}\n"
            f"结论：{'跑赢基准（仍须通过样本外验证）' if comparison.excess_return > 0 else '未跑赢基准，不作为主动交易候选'}"
        )
        if args.output:
            path = write_comparison_report(args.output, {
                "symbols": symbols, "benchmark": args.benchmark, "top_n": args.top_n, "lookback": args.lookback,
                "rebalance_every": args.rebalance_every, "cash": args.cash, "commission": args.commission,
                "min_commission": args.min_commission, "stamp_duty": args.stamp_duty, "slippage_bps": args.slippage_bps,
                "min_history": args.min_history, "min_avg_amount": args.min_avg_amount,
            }, result, comparison)
            print(f"研究报告：{path}")
    finally:
        conn.close()


def run_candidates(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        picks = momentum_candidates(conn, args.as_of, args.top_n, args.lookback, args.min_history, args.min_avg_amount)
        if not picks:
            print("没有满足当前质量门槛的候选股。")
            return
        print(f"候选池（{picks[0].as_of}，仅供研究与模拟）：")
        for number, item in enumerate(picks, start=1):
            print(f"{number:>2}. {item.symbol} {item.name} | {args.lookback} 日动量 {item.momentum:.2%} | 平均成交额 {item.average_amount / 1e8:.2f} 亿")
        if args.save:
            key = f"momentum-{args.lookback}-history-{args.min_history}-amount-{args.min_avg_amount:g}-top-{args.top_n}"
            print(f"已固化候选快照：{save_snapshot(conn, key, picks)} 只（{key}）")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps([item.__dict__ for item in picks], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"候选池文件：{args.output}")
    finally:
        conn.close()


def run_snapshot_status(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        days, rows, latest = snapshot_status(conn)
        print(f"候选池历史快照：{days} 个交易日，{rows} 条记录，最新：{latest or '-'}")
    finally:
        conn.close()


def print_paper_status(status: Any) -> None:
    print(f"模拟账户：{status.account}\n估值日：{status.as_of}\n初始资金：{status.initial_cash:,.2f}\n现金：{status.cash:,.2f}\n持仓市值：{status.market_value:,.2f}\n总权益：{status.total_value:,.2f}\n持仓数：{status.positions}")


def run_paper_init(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        create_account(conn, args.name, args.cash)
        print(f"已创建本地模拟账户：{args.name}（{args.cash:,.2f}）。未连接券商，未产生任何订单。")
    finally:
        conn.close()


def run_paper_status(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        print_paper_status(account_status(conn, args.name, args.as_of))
    finally:
        conn.close()


def run_paper_mark(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        print_paper_status(mark_account(conn, args.name, args.as_of))
        print("已写入本地模拟净值记录。")
    finally:
        conn.close()


def run_paper_auto_init(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        create_account(conn, args.name, args.cash)
        enable_auto_strategy(conn, args.name, top_n=args.top_n, rebalance_every=args.rebalance_every)
        print(f"已创建自动模拟账户：{args.name}（{args.cash:,.2f} 元），持有前 {args.top_n}，"
              f"每 {args.rebalance_every} 个交易日调仓。仅本地记账，不连接券商。")
    finally:
        conn.close()


def run_paper_rebalance(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        trade_date = args.as_of or audit_database(conn).latest_day
        if not trade_date:
            raise ValueError("没有完整覆盖交易日，无法模拟调仓")
        result = rebalance_account(conn, args.name, trade_date)
        print(f"模拟调仓：{result.note}")
        print_paper_status(result.status)
    finally:
        conn.close()


def run_minute_experiment(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        trade_date = args.as_of or audit_database(conn).latest_day
        if not trade_date:
            raise ValueError("没有完整覆盖交易日，无法运行分钟执行实验")
        result = run_execution_experiment(conn, trade_date, args.top_n)
        if result.error:
            print(f"15分钟执行实验暂不可用：{result.error}")
            return
        print(f"15分钟执行实验：信号日 {result.signal_date}，成交观察日 {result.trade_date}")
        for item in result.decisions:
            delayed = "-" if item.delayed_price is None else f"{item.delayed_price:.3f}"
            print(f"{item.symbol} {'通过' if item.accepted else '过滤'} | 开盘 {item.baseline_open:.3f} | "
                  f"09:45 {delayed} | {item.reason}")
    finally:
        conn.close()


def run_enrich(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        audit = audit_database(conn)
        if not audit.latest_day:
            raise ValueError("没有完整覆盖交易日，无法固化行业快照")
        financial = 0 if args.industry_only else update_financials(conn, args.start, args.workers)
        industry = 0 if args.financial_only else update_industries(conn, audit.latest_day, args.workers)
        print(f"扩展数据更新完成：财务报告 {financial} 条，行业成员关系 {industry} 条。")
    finally:
        conn.close()


def run_enrich_status(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        status = enrichment_status(conn)
        print(f"财务报告：{status.financial_rows} 条，{status.financial_symbols} 只股票，最新公告 {status.latest_notice or '-'}\n"
              f"行业快照：{status.industry_dates} 个日期，{status.industry_memberships} 条关系，最新 {status.latest_industry_date or '-'}")
    finally:
        conn.close()


def run_paper_morning(args: argparse.Namespace) -> None:
    now = datetime.now(BEIJING)
    updates: tuple[PaperRebalance, ...] = ()
    error: str | None = None
    if args.dry_run:
        push_card(args, build_morning_card(now, (), None))
        return
    conn = connect(args.db)
    try:
        updates = execute_pending_plans(conn, now.date().isoformat())
    except Exception as caught:
        error = str(caught)[:ERROR_MAX_CHARS]
        LOG.error("09:45模拟执行失败：%s", error)
    finally:
        conn.close()
    push_card(args, build_morning_card(now, updates, error))
    if error:
        raise SystemExit(1)


def run_check(args: argparse.Namespace) -> None:
    symbols = tuple(item.strip() for item in args.symbols.split(",") if item.strip())
    conn = connect(args.db)
    try:
        panel = load_panel(conn, symbols, args.start, args.end)
        validation = walk_forward_rotation(panel, args.train_ratio, top_n=args.top_n, lookback=args.lookback,
            rebalance_every=args.rebalance_every, initial_cash=args.cash, commission_rate=args.commission,
            minimum_commission=args.min_commission, stamp_duty_rate=args.stamp_duty, slippage_bps=args.slippage_bps,
            min_history=args.min_history, min_avg_amount=args.min_avg_amount)
        full = momentum_rotation_backtest(panel, args.top_n, args.lookback, args.rebalance_every, args.cash,
            args.commission, args.min_commission, args.stamp_duty, args.slippage_bps, args.min_history, args.min_avg_amount)
        raw = fetch_kline_bars(index_code(args.benchmark), full.start, full.end, "qfq")
        benchmark = compare_buy_and_hold(args.benchmark, [Bar(day, float(row[1]), float(row[2])) for day, row in raw.items() if len(row) > 2], full.start, full.end, full.total_return)
        robustness = walk_forward_robustness(panel, max_drawdown=args.max_drawdown, min_trades=args.min_trades,
            top_n=args.top_n, lookback=args.lookback, rebalance_every=args.rebalance_every, initial_cash=args.cash,
            commission_rate=args.commission, minimum_commission=args.min_commission, stamp_duty_rate=args.stamp_duty,
            slippage_bps=args.slippage_bps, min_history=args.min_history, min_avg_amount=args.min_avg_amount)
        gate = evaluate_gate(audit_database(conn), validation, benchmark, args.max_drawdown, args.min_trades, robustness)
        print(f"研究准入：{'允许模拟观察' if gate.passed else '拒绝'}")
        for reason in gate.reasons:
            print(f"- {reason}")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({"gate": gate.__dict__, "benchmark": benchmark.__dict__, "validation": {"split_day": validation.split_day, "train": validation.train.__dict__, "validation": validation.validation.__dict__}, "robustness": {"passed": robustness.passed, "reasons": robustness.reasons, "splits": [item.split_day for item in robustness.results]}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"准入报告：{args.output}")
    finally:
        conn.close()


@dataclass(frozen=True)
class DigestOptions:
    """build_digest 的查询参数；与 argparse 解耦，便于直接构造测试。"""
    top_n: int = 10
    lookback: int = 20
    min_history: int = 250
    min_avg_amount: float = 20_000_000
    max_lag_days: int = 7
    as_of: str | None = None


def kline_row_count(db_path: Path) -> int | None:
    """只读连接取行数。刻意不走 connect()——后者会顺带建表、迁移旧库、重建日历。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM kline").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def build_digest(conn: sqlite3.Connection, options: DigestOptions, update_error: str | None,
                 rows_added: int | None, generated_at: datetime) -> DailyDigest:
    """收集候选池与审计结论。读取失败降级为说明性摘要，绝不抛异常。

    卡片本身能送达就是这条链路最重要的信息，所以这里宁可给出「不可用」也不要炸掉。
    """
    audit: DataAudit | None = None
    data_error: str | None = None
    picks: tuple[Candidate, ...] = ()
    snapshot_latest: str | None = None
    try:
        audit = audit_database(conn, options.max_lag_days)
        # 显式传入 as_of：momentum_candidates 在 as_of=None 时会自己再跑一遍
        # audit_database（含全库 integrity_check 与一次全表窗口函数），复用刚拿到的结果。
        picks = tuple(momentum_candidates(
            conn, options.as_of or audit.latest_day, options.top_n,
            options.lookback, options.min_history, options.min_avg_amount))
    except Exception as error:
        data_error = str(error)[:ERROR_MAX_CHARS]
        LOG.warning("读取候选池或审计失败：%s", data_error)
    try:
        snapshot_latest = snapshot_status(conn)[2]
    except Exception:
        pass
    return DailyDigest(generated_at=generated_at, data_date=audit.latest_day if audit else None,
                       update_error=update_error, data_error=data_error, audit=audit, candidates=picks,
                       lookback=options.lookback, min_history=options.min_history,
                       min_avg_amount=options.min_avg_amount, snapshot_latest=snapshot_latest,
                       rows_added=rows_added)


def run_previous_pool_backtest(conn: sqlite3.Connection, data_date: str,
                               report_dir: Path = Path("reports/daily")) -> PreviousPoolBacktest:
    """对上一交易日固化的候选池运行一次标准动量轮动回测并保存报告。"""
    snapshot_date, symbols = previous_snapshot(conn, data_date)
    if not snapshot_date or not symbols:
        raise ValueError("尚无早于本次数据日的候选快照")
    result = momentum_rotation_backtest(
        load_panel(conn, symbols, None, data_date),
        top_n=min(3, len(symbols)), lookback=20, rebalance_every=20,
        initial_cash=100_000, commission_rate=0.0003, minimum_commission=5,
        stamp_duty_rate=0.0005, slippage_bps=5,
        min_history=250, min_avg_amount=20_000_000,
    )
    path = write_backtest_report(
        report_dir / f"previous-candidates-{data_date}.json",
        "previous_candidate_pool_momentum_rotation",
        {
            "snapshot_date": snapshot_date, "symbols": symbols, "top_n": min(3, len(symbols)),
            "lookback": 20, "rebalance_every": 20, "cash": 100_000,
            "commission": 0.0003, "min_commission": 5, "stamp_duty": 0.0005,
            "slippage_bps": 5, "min_history": 250, "min_avg_amount": 20_000_000,
            "end": data_date,
        },
        result,
    )
    return PreviousPoolBacktest(snapshot_date, str(path), result)


def degraded_digest(options: DigestOptions, update_error: str | None, data_error: str,
                    generated_at: datetime) -> DailyDigest:
    """连数据库都打不开时的摘要——仍然要发得出去。"""
    return DailyDigest(generated_at=generated_at, data_date=None, update_error=update_error,
                       data_error=data_error, audit=None, candidates=(),
                       lookback=options.lookback, min_history=options.min_history,
                       min_avg_amount=options.min_avg_amount)


def digest_options(args: argparse.Namespace) -> DigestOptions:
    return DigestOptions(top_n=args.top_n, lookback=args.lookback, min_history=args.min_history,
                         min_avg_amount=args.min_avg_amount, max_lag_days=args.max_lag_days,
                         as_of=args.as_of)


def push_card(args: argparse.Namespace, card: dict[str, Any], note: str = "") -> None:
    """发送一张卡片；--dry-run 只打印，且不需要任何凭据。"""
    if args.dry_run:
        print(json.dumps(card, ensure_ascii=False, indent=2))
        print(f"（--dry-run：未发送{note}）")
        return
    webhook = require_credential(args.webhook, WEBHOOK_ENV, "飞书 Webhook 地址")
    send_card(NotifyTarget(webhook, credential(args.secret, SECRET_ENV)), card)


def run_daily(args: argparse.Namespace) -> None:
    """每日全流程：更新数据 → 审计 → 候选池 → 推送飞书。

    无论中间哪一步失败都照常推送：用户看到沉默时无法区分「今天没事」和「整条链路
    已经死了」。退出码区分失败面，让 systemd 能把 unit 标成 failed。
    """
    now = datetime.now(BEIJING)
    update_error: str | None = None
    rows_added: int | None = None
    if args.no_update:
        LOG.info("已指定 --no-update：跳过数据更新，直接用当前库内容推送。")
    else:
        before = kline_row_count(args.db)
        try:
            run_update(args)
        except Exception as error:
            update_error = str(error)[:ERROR_MAX_CHARS]
            LOG.error("数据更新失败：%s", update_error)
        else:
            after = kline_row_count(args.db)
            if before is not None and after is not None:
                rows_added = after - before
            LOG.info("数据更新完成，新增 %s 行日 K。", rows_added)

    options = digest_options(args)
    conn = None
    try:
        conn = connect(args.db)
        digest = build_digest(conn, options, update_error, rows_added, now)
        if digest.data_date and digest.audit and digest.audit.ok:
            if not args.dry_run:
                try:
                    intraday = run_execution_experiment(conn, digest.data_date)
                    digest = replace(digest, intraday_experiment=intraday)
                    LOG.info("15分钟执行实验：%s", intraday.error or
                             f"接受 {sum(item.accepted for item in intraday.decisions)}/{len(intraday.decisions)}")
                except Exception as error:
                    LOG.warning("15分钟执行实验失败：%s", str(error)[:ERROR_MAX_CHARS])
            try:
                automatic_backtest = run_previous_pool_backtest(conn, digest.data_date)
                digest = replace(digest, previous_pool_backtest=automatic_backtest)
                LOG.info("上一日候选池回测完成：快照 %s，收益 %.2f%%，回撤 %.2f%%，报告 %s",
                         automatic_backtest.snapshot_date,
                         automatic_backtest.result.total_return * 100,
                         automatic_backtest.result.max_drawdown * 100,
                         automatic_backtest.report_path)
            except Exception as error:
                backtest_error = str(error)[:ERROR_MAX_CHARS]
                digest = replace(digest, backtest_error=backtest_error)
                LOG.warning("上一日候选池回测暂不可用：%s", backtest_error)
            if not args.dry_run:
                try:
                    statuses = close_enabled_accounts(conn, digest.data_date)
                    paper_updates = tuple(PaperRebalance(
                        status.account, digest.data_date, None, False, 0, 0, 0,
                        "收盘估值已更新", status) for status in statuses)
                    plans = create_evening_plans(conn, digest.data_date)
                    digest = replace(digest, paper_updates=paper_updates, paper_plans=plans)
                    for status in statuses:
                        LOG.info("模拟账户 %s 收盘：权益 %.2f，现金 %.2f，持仓 %d",
                                 status.account, status.total_value, status.cash, status.positions)
                except Exception as error:
                    # 模拟账户是附加研究功能，失败不能让数据健康与候选池播报一起降级。
                    paper_error = str(error)[:ERROR_MAX_CHARS]
                    digest = replace(digest, paper_error=paper_error)
                    LOG.error("自动模拟调仓失败：%s", paper_error)
        elif digest.data_date:
            digest = replace(digest, backtest_error="数据审计未通过，为避免输出不可信结果已跳过")
    except Exception as error:
        LOG.error("打开数据库失败：%s", error)
        digest = degraded_digest(options, update_error, str(error)[:ERROR_MAX_CHARS], now)
    finally:
        if conn is not None:
            conn.close()

    if digest.audit is not None:  # 审计结论同步进 journal，保留原有的取证能力
        for line in audit_report_text(digest.audit, args.max_lag_days).splitlines():
            LOG.info("%s", line)

    notify_error: str | None = None
    try:
        push_card(args, build_card(digest),
                  f"。卡片状态 {digest.status}，标题颜色 {digest.header_template}")
    except Exception as error:
        notify_error = str(error)[:ERROR_MAX_CHARS]
        LOG.error("飞书推送失败：%s", notify_error)

    if update_error and notify_error:
        LOG.error("数据未更新且通知未送达，这是最坏情况。")
        raise SystemExit(3)
    if update_error:
        raise SystemExit(1)
    if notify_error:
        raise SystemExit(2)
    LOG.info("每日播报完成：状态 %s，候选 %d 只。", digest.status, len(digest.candidates))


def run_notify(args: argparse.Namespace) -> None:
    """只推送，不碰数据。--test 发配置自检卡片（连库都不读，可单独验证飞书链路）。"""
    now = datetime.now(BEIJING)
    if args.test:
        card = build_test_card(now, bool(credential(args.secret, SECRET_ENV)))
    else:
        conn = connect(args.db)
        try:
            card = build_card(build_digest(conn, digest_options(args), None, None, now))
        finally:
            conn.close()
    push_card(args, card)
    if not args.dry_run:
        print("已发送，请查看飞书群。")


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(description="WinStock A 股清单与日 K 初始化工具")
    app.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径（默认 data/winstock.db）")
    sub = app.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="刷新 A 股清单并分批下载日 K")
    init.add_argument("--start", default="2024-01-01")
    init.add_argument("--end", default=date.today().isoformat())
    init.add_argument("--batch-size", type=int, default=100)
    init.add_argument("--workers", type=int, default=6)
    update = sub.add_parser("update", help="增量刷新日 K（重抓最后一天并补齐新交易日）")
    update.add_argument("--end", default=date.today().isoformat())
    update.add_argument("--bootstrap-start", default="2024-01-01", help="空数据库首次更新的起始日")
    update.add_argument("--batch-size", type=int, default=100)
    update.add_argument("--workers", type=int, default=6)
    update.add_argument("--no-snapshot", action="store_true", help="仅更新数据，不自动固化当天候选池")
    sub.add_parser("list", help="仅刷新 A 股清单")
    sub.add_parser("status", help="查看本地数据状态")
    audit = sub.add_parser("audit", help="审计数据完整性和覆盖率；回测前建议运行")
    audit.add_argument("--max-lag-days", type=int, default=7, help="个股相对最新数据允许落后天数，默认 7")
    backtest = sub.add_parser("backtest", help="运行日频双均线研究回测（昨日收盘信号、今日开盘成交）")
    backtest.add_argument("symbol", help="A 股代码，例如 600000")
    backtest.add_argument("--start", help="回测起始日，默认使用全量数据")
    backtest.add_argument("--end", help="回测结束日，默认使用全量数据")
    backtest.add_argument("--fast", type=int, default=20, help="快均线窗口，默认 20")
    backtest.add_argument("--slow", type=int, default=60, help="慢均线窗口，默认 60")
    backtest.add_argument("--cash", type=float, default=100_000, help="初始资金，默认 100000")
    backtest.add_argument("--commission", type=float, default=0.0003, help="佣金费率，默认万三")
    backtest.add_argument("--min-commission", type=float, default=5, help="单笔最低佣金，默认 5 元")
    backtest.add_argument("--stamp-duty", type=float, default=0.0005, help="卖出印花税率，默认万五")
    backtest.add_argument("--slippage-bps", type=float, default=5, help="单边滑点（bp），默认 5")
    backtest.add_argument("--output", type=Path, help="将可复现的 JSON 研究报告写入此路径")
    rotation = sub.add_parser("rotation", help="运行指定股票池的日频动量轮动回测")
    rotation.add_argument("--symbols", required=True, help="逗号分隔的股票池，例如 600000,000001,300750")
    rotation.add_argument("--start", help="回测起始日，默认使用全量数据")
    rotation.add_argument("--end", help="回测结束日，默认使用全量数据")
    rotation.add_argument("--top-n", type=int, default=3, help="持有动量最高的股票数量，默认 3")
    rotation.add_argument("--lookback", type=int, default=20, help="动量计算窗口，默认 20 日")
    rotation.add_argument("--rebalance-every", type=int, default=20, help="调仓间隔，默认 20 日")
    rotation.add_argument("--cash", type=float, default=100_000, help="初始资金，默认 100000")
    rotation.add_argument("--commission", type=float, default=0.0003, help="佣金费率，默认万三")
    rotation.add_argument("--min-commission", type=float, default=5, help="单笔最低佣金，默认 5 元")
    rotation.add_argument("--stamp-duty", type=float, default=0.0005, help="卖出印花税率，默认万五")
    rotation.add_argument("--slippage-bps", type=float, default=5, help="单边滑点（bp），默认 5")
    rotation.add_argument("--min-history", type=int, default=250, help="至少具备的历史交易日数，默认 250")
    rotation.add_argument("--min-avg-amount", type=float, default=20_000_000, help="近 lookback 日平均成交额下限（元），默认 2000 万")
    rotation.add_argument("--output", type=Path, help="将可复现的 JSON 研究报告写入此路径")
    validate = sub.add_parser("validate", help="动量轮动的训练/样本外验证，防止只看历史拟合")
    validate.add_argument("--symbols", required=True, help="逗号分隔的股票池，例如 600000,000001,300750")
    validate.add_argument("--start", help="回测起始日，默认使用全量数据")
    validate.add_argument("--end", help="回测结束日，默认使用全量数据")
    validate.add_argument("--train-ratio", type=float, default=0.7, help="训练段比例，默认 0.7")
    validate.add_argument("--top-n", type=int, default=3)
    validate.add_argument("--lookback", type=int, default=20)
    validate.add_argument("--rebalance-every", type=int, default=20)
    validate.add_argument("--cash", type=float, default=100_000)
    validate.add_argument("--commission", type=float, default=0.0003)
    validate.add_argument("--min-commission", type=float, default=5)
    validate.add_argument("--stamp-duty", type=float, default=0.0005)
    validate.add_argument("--slippage-bps", type=float, default=5)
    validate.add_argument("--min-history", type=int, default=250)
    validate.add_argument("--min-avg-amount", type=float, default=20_000_000)
    validate.add_argument("--output", type=Path, help="将训练/验证结果写入 JSON 报告")
    compare = sub.add_parser("compare", help="将动量轮动与市场基准的买入持有收益比较")
    compare.add_argument("--symbols", required=True, help="逗号分隔的股票池")
    compare.add_argument("--benchmark", default="000300", help="基准指数，默认沪深 300（000300）")
    compare.add_argument("--start", help="回测起始日，默认使用全量数据")
    compare.add_argument("--end", help="回测结束日，默认使用全量数据")
    compare.add_argument("--top-n", type=int, default=3)
    compare.add_argument("--lookback", type=int, default=20)
    compare.add_argument("--rebalance-every", type=int, default=20)
    compare.add_argument("--cash", type=float, default=100_000)
    compare.add_argument("--commission", type=float, default=0.0003)
    compare.add_argument("--min-commission", type=float, default=5)
    compare.add_argument("--stamp-duty", type=float, default=0.0005)
    compare.add_argument("--slippage-bps", type=float, default=5)
    compare.add_argument("--min-history", type=int, default=250)
    compare.add_argument("--min-avg-amount", type=float, default=20_000_000)
    compare.add_argument("--output", type=Path, help="将策略与基准比较写入 JSON 报告")
    candidates = sub.add_parser("candidates", help="生成通过基础质量门槛的日频研究候选池，不产生交易指令")
    candidates.add_argument("--as-of", help="截止交易日，默认最近完整覆盖日")
    candidates.add_argument("--top-n", type=int, default=10)
    candidates.add_argument("--lookback", type=int, default=20)
    candidates.add_argument("--min-history", type=int, default=250)
    candidates.add_argument("--min-avg-amount", type=float, default=20_000_000)
    candidates.add_argument("--output", type=Path, help="将候选池写入 JSON 文件")
    candidates.add_argument("--save", action="store_true", help="将本次候选池固化为带日期的历史快照")
    sub.add_parser("snapshot-status", help="查看已固化的候选池历史快照")
    paper_init = sub.add_parser("paper-init", help="创建纯本地模拟账户；不连接券商")
    paper_init.add_argument("--name", default="default")
    paper_init.add_argument("--cash", type=float, default=100_000)
    paper_status = sub.add_parser("paper-status", help="查看本地模拟账户估值")
    paper_status.add_argument("--name", default="default")
    paper_status.add_argument("--as-of")
    paper_mark = sub.add_parser("paper-mark", help="写入本地模拟账户的当日净值")
    paper_mark.add_argument("--name", default="default")
    paper_mark.add_argument("--as-of")
    paper_auto_init = sub.add_parser("paper-auto-init", help="创建按候选快照自动调仓的本地模拟账户")
    paper_auto_init.add_argument("--name", default="momentum-10k")
    paper_auto_init.add_argument("--cash", type=float, default=10_000)
    paper_auto_init.add_argument("--top-n", type=int, default=3)
    paper_auto_init.add_argument("--rebalance-every", type=int, default=20)
    paper_rebalance = sub.add_parser("paper-rebalance", help="按上一候选快照执行一次本地模拟调仓")
    paper_rebalance.add_argument("--name", default="momentum-10k")
    paper_rebalance.add_argument("--as-of", help="模拟成交日，默认最近完整覆盖日")
    minute_experiment = sub.add_parser("minute-experiment", help="运行开盘价与09:45成交过滤的影子A/B实验")
    minute_experiment.add_argument("--as-of", help="观察交易日，默认最近完整覆盖日")
    minute_experiment.add_argument("--top-n", type=int, default=3)
    enrich = sub.add_parser("enrich", help="更新按公告日保存的财务历史和带日期的行业快照")
    enrich.add_argument("--start", default="2021-01-01", help="财务报告期起点，默认2021-01-01")
    enrich.add_argument("--workers", type=int, default=6)
    enrich.add_argument("--financial-only", action="store_true")
    enrich.add_argument("--industry-only", action="store_true")
    sub.add_parser("enrich-status", help="查看财务与行业扩展数据状态")
    paper_morning = sub.add_parser("paper-morning", help="09:45按前夜计划执行模拟成交并推送飞书")
    paper_morning.add_argument("--webhook", help=f"飞书机器人 Webhook，默认读 {WEBHOOK_ENV}")
    paper_morning.add_argument("--secret", help=f"飞书签名密钥，默认读 {SECRET_ENV}")
    paper_morning.add_argument("--dry-run", action="store_true")
    check = sub.add_parser("check", help="自动检查策略是否仅可进入模拟观察；绝不输出实盘许可")
    check.add_argument("--symbols", required=True, help="逗号分隔的股票池")
    check.add_argument("--benchmark", default="000300")
    check.add_argument("--start")
    check.add_argument("--end")
    check.add_argument("--train-ratio", type=float, default=0.7)
    check.add_argument("--top-n", type=int, default=3)
    check.add_argument("--lookback", type=int, default=20)
    check.add_argument("--rebalance-every", type=int, default=20)
    check.add_argument("--cash", type=float, default=100_000)
    check.add_argument("--commission", type=float, default=0.0003)
    check.add_argument("--min-commission", type=float, default=5)
    check.add_argument("--stamp-duty", type=float, default=0.0005)
    check.add_argument("--slippage-bps", type=float, default=5)
    check.add_argument("--min-history", type=int, default=250)
    check.add_argument("--min-avg-amount", type=float, default=20_000_000)
    check.add_argument("--max-drawdown", type=float, default=-0.20, help="样本外最大回撤警戒线，默认 -0.20")
    check.add_argument("--min-trades", type=int, default=10)
    check.add_argument("--output", type=Path)
    daily = sub.add_parser("daily", help="每日全流程：更新日 K、审计、生成候选池并推送到飞书群")
    daily.add_argument("--webhook", help=f"飞书机器人 Webhook 地址，默认读环境变量 {WEBHOOK_ENV}")
    daily.add_argument("--secret", help=f"飞书机器人签名密钥（开启签名校验时必填），默认读环境变量 {SECRET_ENV}")
    daily.add_argument("--end", default=datetime.now(BEIJING).date().isoformat(), help="更新截止日，默认北京时间今天")
    daily.add_argument("--bootstrap-start", default="2024-01-01", help="空数据库首次更新的起始日")
    daily.add_argument("--batch-size", type=int, default=100)
    daily.add_argument("--workers", type=int, default=4, help="并发下载线程数，默认 4")
    daily.add_argument("--no-snapshot", action="store_true", help="仅更新数据，不自动固化当天候选池")
    daily.add_argument("--no-update", action="store_true", help="跳过数据更新，只用当前库内容推送（验证推送链路用）")
    daily.add_argument("--dry-run", action="store_true", help="只把卡片内容打印到终端，不发送")
    daily.add_argument("--top-n", type=int, default=10, help="推送的候选股数量，默认 10")
    daily.add_argument("--lookback", type=int, default=20, help="动量计算窗口，默认 20 个交易日")
    daily.add_argument("--min-history", type=int, default=250, help="至少具备的历史交易日数，默认 250")
    daily.add_argument("--min-avg-amount", type=float, default=20_000_000, help="近 lookback 日平均成交额下限（元），默认 2000 万")
    daily.add_argument("--max-lag-days", type=int, default=7, help="个股相对最新数据允许落后天数，默认 7")
    daily.add_argument("--as-of", help="截止交易日，默认最近完整覆盖日")
    notify = sub.add_parser("notify", help="只推送一张飞书卡片，不更新任何数据；用于验证配置")
    notify.add_argument("--test", action="store_true", help="发送配置自检卡片（不读数据库，可单独验证飞书链路）")
    notify.add_argument("--webhook", help=f"飞书机器人 Webhook 地址，默认读环境变量 {WEBHOOK_ENV}")
    notify.add_argument("--secret", help=f"飞书机器人签名密钥，默认读环境变量 {SECRET_ENV}")
    notify.add_argument("--dry-run", action="store_true", help="只把卡片内容打印到终端，不发送")
    notify.add_argument("--top-n", type=int, default=10)
    notify.add_argument("--lookback", type=int, default=20)
    notify.add_argument("--min-history", type=int, default=250)
    notify.add_argument("--min-avg-amount", type=float, default=20_000_000)
    notify.add_argument("--max-lag-days", type=int, default=7)
    notify.add_argument("--as-of")
    return app


def main() -> None:
    args = parser().parse_args()
    if getattr(args, "batch_size", 1) < 1 or getattr(args, "workers", 1) < 1:
        raise SystemExit("--batch-size 和 --workers 必须为正整数")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "init":
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end)
            if start > end:
                raise ValueError("--start 不能晚于 --end")
        if args.command in ("update", "daily"):
            if date.fromisoformat(args.bootstrap_start) > date.fromisoformat(args.end):
                raise ValueError("--bootstrap-start 不能晚于 --end")
        if args.command == "enrich":
            date.fromisoformat(args.start)
            if args.financial_only and args.industry_only:
                raise ValueError("--financial-only 与 --industry-only 不能同时使用")
        {"init": run_init, "update": run_update, "list": run_list, "status": run_status, "audit": run_audit, "enrich": run_enrich, "enrich-status": run_enrich_status, "backtest": run_backtest, "rotation": run_rotation, "validate": run_validate, "compare": run_compare, "candidates": run_candidates, "snapshot-status": run_snapshot_status, "paper-init": run_paper_init, "paper-status": run_paper_status, "paper-mark": run_paper_mark, "paper-auto-init": run_paper_auto_init, "paper-rebalance": run_paper_rebalance, "paper-morning": run_paper_morning, "minute-experiment": run_minute_experiment, "check": run_check, "daily": run_daily, "notify": run_notify}[args.command](args)
    except Exception as error:
        LOG.error("%s", error)
        raise SystemExit(1) from error
