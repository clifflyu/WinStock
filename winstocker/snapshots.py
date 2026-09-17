"""Persist dated research universes for forward-looking validation."""
from __future__ import annotations

import sqlite3
from typing import Iterable

from .candidates import Candidate


SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_snapshots (
    strategy_key TEXT NOT NULL,
    as_of TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    momentum REAL NOT NULL,
    average_amount REAL NOT NULL,
    history_days INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (strategy_key, as_of, symbol)
);
"""


def save_snapshot(conn: sqlite3.Connection, strategy_key: str, candidates: Iterable[Candidate]) -> int:
    """固化某一天的候选池。同一 (策略, 日期) 视为一次计算：先清空再写入。

    只 UPSERT 是不够的——筛选条件变化后掉出名单的股票会残留下来，让这一天同时
    含有两套互斥的结果（实测就出现过 10 只变 11 只）。清空后重写保证该日结果自洽。
    """
    candidates = list(candidates)
    conn.executescript(SNAPSHOT_SCHEMA)
    with conn:
        for as_of in {item.as_of for item in candidates}:
            conn.execute("DELETE FROM candidate_snapshots WHERE strategy_key = ? AND as_of = ?", (strategy_key, as_of))
        conn.executemany(
            """INSERT INTO candidate_snapshots(strategy_key, as_of, symbol, name, momentum, average_amount, history_days)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(strategy_key, as_of, symbol) DO UPDATE SET
                 name=excluded.name, momentum=excluded.momentum, average_amount=excluded.average_amount,
                 history_days=excluded.history_days, created_at=CURRENT_TIMESTAMP""",
            [(strategy_key, item.as_of, item.symbol, item.name, item.momentum, item.average_amount, item.history_days) for item in candidates],
        )
    return len(candidates)


def snapshot_status(conn: sqlite3.Connection) -> tuple[int, int, str | None]:
    conn.executescript(SNAPSHOT_SCHEMA)
    return conn.execute("SELECT COUNT(DISTINCT as_of), COUNT(*), MAX(as_of) FROM candidate_snapshots").fetchone()
