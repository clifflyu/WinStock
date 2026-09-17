from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import math
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .audit import audit_database
from .backtest import dual_ma_backtest, load_bars, load_panel, momentum_rotation_backtest, walk_forward_rotation
from .reporting import write_backtest_report, write_walk_forward_report

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
    migrate_legacy(conn)
    conn.executescript(KLINE_SCHEMA)
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
        LOG.info("初始化结束：成功 %d，失败 %d。运行 `python -m winstocker status` 查看详情。", successful, failed)
    finally:
        conn.close()


def incremental_start(conn: sqlite3.Connection, end: str, bootstrap_start: str) -> str:
    """Re-fetch the latest stored day so corrections are UPSERTed, then append new days."""
    latest = conn.execute("SELECT MAX(trade_date) FROM kline").fetchone()[0]
    if not latest:
        return bootstrap_start
    return min(latest, end)


def run_update(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        LOG.info("正在刷新 A 股清单…")
        securities = fetch_securities()
        if not securities:
            raise RuntimeError("A 股清单为空，终止更新。")
        save_securities(conn, securities)
        start = incremental_start(conn, args.end, args.bootstrap_start)
        if date.fromisoformat(start) > date.fromisoformat(args.end):
            raise ValueError("数据库最新日期晚于 --end；请指定更晚的 --end")
        ids = security_ids(conn)
        LOG.info("增量更新 %s 至 %s，共 %d 只 A 股。", start, args.end, len(securities))
        successful, failed = 0, 0
        for number, group in enumerate(batches(securities, args.batch_size), start=1):
            rows_to_save: list[tuple[Any, ...]] = []
            errors: list[tuple[str, str, str]] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(fetch_kline, item, start, args.end): item for item in group}
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


def run_audit(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        report = audit_database(conn, args.max_lag_days)
        verdict = "通过" if report.ok else "需检查（不要直接相信回测结果）"
        print(
            f"数据审计：{verdict}\n数据库完整性：{report.integrity}\n"
            f"活跃证券：{report.active_securities}\n有日线证券：{report.symbols_with_bars}\n"
            f"日线总数：{report.rows}\n完整覆盖截至：{report.latest_day or '-'}（{report.latest_day_symbols} 只）\n"
            f"最新观测日：{report.newest_observed_day or '-'}（{report.newest_observed_symbols} 只）\n"
            f"无日线证券：{report.no_data_symbols}\n"
            f"落后最新日超过 {args.max_lag_days} 天：{report.lagging_symbols} 只\n"
            f"下载失败待重试：{report.failures}"
        )
    finally:
        conn.close()


def run_backtest(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        bars = load_bars(conn, args.symbol, args.start, args.end)
        result = dual_ma_backtest(
            bars, args.symbol, args.fast, args.slow, args.cash, args.commission,
            args.min_commission, args.stamp_duty, args.slippage_bps,
        )
        annualized = "-" if result.annualized_return is None else f"{result.annualized_return:.2%}"
        print(
            f"策略：双均线（{args.fast}/{args.slow}），{result.symbol}\n"
            f"区间：{result.start} 至 {result.end}\n"
            f"初始资金：{result.initial_cash:,.2f}\n最终权益：{result.final_value:,.2f}\n"
            f"总收益：{result.total_return:.2%}\n年化收益：{annualized}\n"
            f"最大回撤：{result.max_drawdown:.2%}\n成交笔数：{result.trades}\n"
            f"涨停未买入：{result.blocked_buys}\n跌停未卖出：{result.blocked_sells}"
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
        )
        annualized = "-" if result.annualized_return is None else f"{result.annualized_return:.2%}"
        print(
            f"策略：{args.lookback} 日动量前 {args.top_n}，每 {args.rebalance_every} 日调仓\n"
            f"股票池：{','.join(result.symbols)}\n区间：{result.start} 至 {result.end}\n"
            f"初始资金：{result.initial_cash:,.2f}\n最终权益：{result.final_value:,.2f}\n"
            f"总收益：{result.total_return:.2%}\n年化收益：{annualized}\n最大回撤：{result.max_drawdown:.2%}\n"
            f"调仓次数：{result.rebalances}\n成交笔数：{result.trades}\n"
            f"涨停未买入：{result.blocked_buys}\n跌停未卖出：{result.blocked_sells}"
        )
        if args.output:
            path = write_backtest_report(args.output, "momentum_rotation", {
                "symbols": symbols, "top_n": args.top_n, "lookback": args.lookback,
                "rebalance_every": args.rebalance_every, "cash": args.cash, "commission": args.commission,
                "min_commission": args.min_commission, "stamp_duty": args.stamp_duty,
                "slippage_bps": args.slippage_bps,
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
            stamp_duty_rate=args.stamp_duty, slippage_bps=args.slippage_bps,
        )
        def summary(label: str, item: Any) -> str:
            annualized = "-" if item.annualized_return is None else f"{item.annualized_return:.2%}"
            return f"{label}（{item.start} 至 {item.end}）：收益 {item.total_return:.2%}，年化 {annualized}，回撤 {item.max_drawdown:.2%}，成交 {item.trades} 笔"
        print(f"样本外验证切分日：{result.split_day}\n{summary('训练段', result.train)}\n{summary('验证段', result.validation)}")
        if args.output:
            path = write_walk_forward_report(args.output, {
                "symbols": symbols, "train_ratio": args.train_ratio, "top_n": args.top_n,
                "lookback": args.lookback, "rebalance_every": args.rebalance_every, "cash": args.cash,
                "commission": args.commission, "min_commission": args.min_commission,
                "stamp_duty": args.stamp_duty, "slippage_bps": args.slippage_bps,
            }, result)
            print(f"研究报告：{path}")
    finally:
        conn.close()


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
    validate.add_argument("--output", type=Path, help="将训练/验证结果写入 JSON 报告")
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
        if args.command == "update":
            if date.fromisoformat(args.bootstrap_start) > date.fromisoformat(args.end):
                raise ValueError("--bootstrap-start 不能晚于 --end")
        {"init": run_init, "update": run_update, "list": run_list, "status": run_status, "audit": run_audit, "backtest": run_backtest, "rotation": run_rotation, "validate": run_validate}[args.command](args)
    except Exception as error:
        LOG.error("%s", error)
        raise SystemExit(1) from error
