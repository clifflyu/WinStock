import sqlite3
import unittest

from winstocker.candidates import Candidate
from winstocker.intraday import MinuteBar, evaluate_first_bar, run_execution_experiment
from winstocker.snapshots import DEFAULT_STRATEGY_KEY, save_snapshot


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


if __name__ == "__main__":
    unittest.main()
