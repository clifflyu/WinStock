import sqlite3
import unittest
from unittest.mock import patch

from winstocker.candidates import Candidate
from winstocker.intraday import MinuteBar, evaluate_first_bar, run_execution_experiment
from winstocker.snapshots import DEFAULT_STRATEGY_KEY, save_snapshot
from winstocker.paper import create_account, enable_auto_strategy
from winstocker.paper_flow import create_evening_plans, execute_pending_plans


class IntradayTests(unittest.TestCase):
    def test_first_bar_filter_rejects_large_gap_and_accepts_normal_bar(self):
        normal = MinuteBar("600000", "2024-01-03 09:45", 10.1, 10.15, 10.2, 10.0, 1000)
        accepted = evaluate_first_bar("600000", 10.1, 10.0, normal)
        self.assertTrue(accepted.accepted)
        gap = MinuteBar("600000", "2024-01-03 09:45", 10.5, 10.5, 10.6, 10.4, 1000)
        rejected = evaluate_first_bar("600000", 10.5, 10.0, gap)
        self.assertFalse(rejected.accepted)
        self.assertIn("高开", rejected.reason)

    def test_experiment_uses_previous_snapshot_and_persists_decision(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE daily_kline (symbol TEXT, trade_date TEXT, open REAL, close REAL)")
        conn.executemany("INSERT INTO daily_kline VALUES (?, ?, ?, ?)", [
            ("600000", "2024-01-02", 10, 10),
            ("600000", "2024-01-03", 10.1, 10.2),
        ])
        save_snapshot(conn, DEFAULT_STRATEGY_KEY, [
            Candidate("600000", "测试", "2024-01-02", 0.1, 30_000_000, 250)
        ])

        def fetcher(symbol):
            return [MinuteBar(symbol, "2024-01-03 09:45", 10.1, 10.15, 10.2, 10.0, 1000)]

        result = run_execution_experiment(conn, "2024-01-03", fetcher=fetcher)
        self.assertEqual(result.signal_date, "2024-01-02")
        self.assertTrue(result.decisions[0].accepted)
        stored = conn.execute("SELECT accepted, delayed_price FROM execution_experiments").fetchone()
        self.assertEqual(stored[0], 1)
        self.assertAlmostEqual(stored[1], 10.15)

    def test_evening_plan_executes_once_at_0945(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE daily_kline (symbol TEXT, trade_date TEXT, open REAL, close REAL, amount REAL);
            CREATE TABLE trading_calendar (trade_date TEXT PRIMARY KEY);
            INSERT INTO trading_calendar VALUES ('2024-01-02'),('2024-01-03');
            INSERT INTO daily_kline VALUES ('600000','2024-01-02',10,10,30000000);
        """)
        save_snapshot(conn, DEFAULT_STRATEGY_KEY, [
            Candidate("600000", "测试", "2024-01-02", .1, 30_000_000, 250)])
        create_account(conn, "auto", 10_000)
        enable_auto_strategy(conn, "auto", top_n=1)
        plan = create_evening_plans(conn, "2024-01-02")[0]
        self.assertTrue(plan.pending)
        bars = [MinuteBar("600000", "2024-01-03 09:45", 10.1, 10.15, 10.2, 10, 1000)]
        with patch("winstocker.paper_flow.fetch_m15", return_value=bars):
            first = execute_pending_plans(conn, "2024-01-03")
            second = execute_pending_plans(conn, "2024-01-03")
        self.assertEqual(first[0].buys, 1)
        self.assertEqual(len(first[0].fills), 1)
        self.assertEqual(second, ())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
