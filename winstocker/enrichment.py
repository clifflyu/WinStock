"""Point-in-time fundamentals and dated industry snapshots from Eastmoney."""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
import json
import math
import sqlite3
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


FIN_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
LIST_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
ENRICHMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS financial_reports (
    symbol TEXT NOT NULL,
    report_date TEXT NOT NULL,
    notice_date TEXT NOT NULL,
    report_type TEXT,
    org_type TEXT,
    roe REAL,
    revenue REAL,
    revenue_yoy REAL,
    net_profit REAL,
    net_profit_yoy REAL,
    operating_cashflow REAL,
    debt_ratio REAL,
    total_assets REAL,
    total_equity REAL,
    PRIMARY KEY(symbol, report_date, notice_date)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_financial_notice ON financial_reports(notice_date, symbol);
CREATE TABLE IF NOT EXISTS industry_boards (
    board_code TEXT PRIMARY KEY,
    board_name TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS industry_memberships (
    as_of TEXT NOT NULL,
    board_code TEXT NOT NULL,
    symbol TEXT NOT NULL,
    PRIMARY KEY(as_of, board_code, symbol)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_industry_symbol ON industry_memberships(as_of, symbol);
"""


@dataclass(frozen=True)
class EnrichmentStatus:
    financial_rows: int
    financial_symbols: int
    latest_notice: str | None
    industry_dates: int
    industry_memberships: int
    latest_industry_date: str | None


def _json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": UA,
                      "Referer": "https://data.eastmoney.com/"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("success") is False or payload.get("data") is None and payload.get("result") is None:
        raise RuntimeError(payload.get("message") or payload.get("msg") or "接口未返回数据")
    return payload


def _groups(values: Iterable[str], size: int) -> Iterable[tuple[str, ...]]:
    batch: list[str] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield tuple(batch); batch = []
    if batch:
        yield tuple(batch)


def fetch_financial_batch(symbols: tuple[str, ...], start: str) -> list[dict[str, Any]]:
    quoted = ",".join(f'"{symbol}"' for symbol in symbols)
    filter_ = f"(SECURITY_CODE in ({quoted}))(REPORT_DATE>='{start}')"
    base = {"reportName": "RPT_F10_FINANCE_MAINFINADATA", "columns": "ALL",
            "filter": filter_, "pageSize": 500, "sortColumns": "REPORT_DATE",
            "sortTypes": -1, "source": "HSF10", "client": "PC"}
    first = _json(FIN_URL, {**base, "pageNumber": 1}).get("result") or {}
    rows = list(first.get("data") or [])
    for page in range(2, int(first.get("pages") or 1) + 1):
        rows.extend((_json(FIN_URL, {**base, "pageNumber": page}).get("result") or {}).get("data") or [])
    return rows


def update_financials(conn: sqlite3.Connection, start: str = "2021-01-01", workers: int = 6) -> int:
    conn.executescript(ENRICHMENT_SCHEMA)
    symbols = [row[0] for row in conn.execute("SELECT symbol FROM securities WHERE is_active=1 ORDER BY symbol")]
    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_financial_batch, group, start) for group in _groups(symbols, 50)]
        for future in concurrent.futures.as_completed(futures):
            rows.extend(future.result())
    active = set(symbols)
    values = []
    for row in rows:
        symbol = str(row.get("SECURITY_CODE") or "")
        if symbol not in active or not row.get("REPORT_DATE") or not row.get("NOTICE_DATE"):
            continue
        values.append((symbol, str(row["REPORT_DATE"])[:10], str(row["NOTICE_DATE"])[:10],
                       row.get("REPORT_TYPE"), row.get("ORG_TYPE"), row.get("ROEJQ"),
                       row.get("TOTALOPERATEREVE"), row.get("TOTALOPERATEREVETZ"),
                       row.get("PARENTNETPROFIT"), row.get("PARENTNETPROFITTZ"),
                       row.get("NETCASH_OPERATE_PK"), row.get("ZCFZL"),
                       row.get("TOTAL_ASSETS_PK"), row.get("TOTAL_EQUITY_PK")))
    with conn:
        conn.executemany("""INSERT INTO financial_reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol,report_date,notice_date) DO UPDATE SET
            report_type=excluded.report_type,org_type=excluded.org_type,roe=excluded.roe,
            revenue=excluded.revenue,revenue_yoy=excluded.revenue_yoy,net_profit=excluded.net_profit,
            net_profit_yoy=excluded.net_profit_yoy,operating_cashflow=excluded.operating_cashflow,
            debt_ratio=excluded.debt_ratio,total_assets=excluded.total_assets,total_equity=excluded.total_equity""", values)
    return len(values)


def fetch_industry_boards() -> list[tuple[str, str]]:
    base = {"pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
            "fs": "m:90+t:2+f:!50", "fields": "f12,f14"}
    first = _json(LIST_URL, {**base, "pn": 1})["data"]
    rows = list(first.get("diff") or [])
    for page in range(2, math.ceil(int(first.get("total") or 0) / 100) + 1):
        rows.extend(_json(LIST_URL, {**base, "pn": page})["data"].get("diff") or [])
    return [(str(row["f12"]), str(row["f14"])) for row in rows if row.get("f12") and row.get("f14")]


def fetch_board_members(board_code: str) -> list[str]:
    base = {"pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
            "fs": f"b:{board_code}+f:!50", "fields": "f12"}
    first = _json(LIST_URL, {**base, "pn": 1})["data"]
    rows = list(first.get("diff") or [])
    for page in range(2, math.ceil(int(first.get("total") or 0) / 100) + 1):
        rows.extend(_json(LIST_URL, {**base, "pn": page})["data"].get("diff") or [])
    return [str(row["f12"]) for row in rows if row.get("f12")]


def update_industries(conn: sqlite3.Connection, as_of: str, workers: int = 8) -> int:
    conn.executescript(ENRICHMENT_SCHEMA)
    boards = fetch_industry_boards()
    active = {row[0] for row in conn.execute("SELECT symbol FROM securities WHERE is_active=1")}
    memberships: list[tuple[str, str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_board_members, code): code for code, _ in boards}
        for future in concurrent.futures.as_completed(futures):
            code = futures[future]
            memberships.extend((as_of, code, symbol) for symbol in future.result() if symbol in active)
    with conn:
        conn.executemany("""INSERT INTO industry_boards(board_code,board_name) VALUES (?,?)
            ON CONFLICT(board_code) DO UPDATE SET board_name=excluded.board_name,updated_at=CURRENT_TIMESTAMP""", boards)
        conn.execute("DELETE FROM industry_memberships WHERE as_of=?", (as_of,))
        conn.executemany("INSERT INTO industry_memberships VALUES (?,?,?)", memberships)
    return len(memberships)


def enrichment_status(conn: sqlite3.Connection) -> EnrichmentStatus:
    conn.executescript(ENRICHMENT_SCHEMA)
    financial = conn.execute("SELECT COUNT(*),COUNT(DISTINCT symbol),MAX(notice_date) FROM financial_reports").fetchone()
    industry = conn.execute("SELECT COUNT(DISTINCT as_of),COUNT(*),MAX(as_of) FROM industry_memberships").fetchone()
    return EnrichmentStatus(*financial, *industry)
