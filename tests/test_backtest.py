import unittest
import sqlite3
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from winstocker.audit import audit_database
from winstocker.cli import incremental_start
from winstocker.reporting import research_warnings, write_backtest_report, write_walk_forward_report
from winstocker.backtest import Bar, dual_ma_backtest, limit_ratio, momentum_rotation_backtest, walk_forward_rotation


class BacktestTests(unittest.TestCase):
    def test_board_limit_rules(self):
        self.assertEqual(limit_ratio("600000"), 0.10)
        self.assertEqual(limit_ratio("300750"), 0.20)
        self.assertEqual(limit_ratio("688001"), 0.20)

    def test_next_open_execution_and_lot_size(self):
        bars = [Bar(f"2024-01-{i:02d}", 10, 10) for i in range(1, 5)]
        bars += [Bar("2024-01-05", 10, 12), Bar("2024-01-06", 10, 12), Bar("2024-01-07", 11, 13)]
        result = dual_ma_backtest(bars, "600000", fast=1, slow=3, initial_cash=10_000, commission_rate=0, minimum_commission=0, stamp_duty_rate=0, slippage_bps=0)
        self.assertEqual(result.trades, 1)
        self.assertGreater(result.final_value, 10_000)

    def test_up_limit_blocks_buy(self):
        bars = [Bar(f"2024-01-{i:02d}", 10, 10) for i in range(1, 5)]
        bars += [Bar("2024-01-05", 10, 12), Bar("2024-01-06", 13.2, 12), Bar("2024-01-07", 12, 12)]
        result = dual_ma_backtest(bars, "600000", fast=1, slow=3, commission_rate=0, minimum_commission=0, stamp_duty_rate=0, slippage_bps=0)
        self.assertEqual(result.blocked_buys, 1)

    def test_rotation_selects_and_buys_the_strongest_stock(self):
        days = [f"2024-01-{i:02d}" for i in range(1, 8)]
        panel = {
            "600000": {day: Bar(day, 10 + i, 10 + i) for i, day in enumerate(days)},
            "000001": {day: Bar(day, 10, 10) for day in days},
        }
        result = momentum_rotation_backtest(panel, top_n=1, lookback=3, rebalance_every=10, initial_cash=10_000, commission_rate=0, minimum_commission=0, stamp_duty_rate=0, slippage_bps=0)
        self.assertEqual(result.rebalances, 1)
        self.assertEqual(result.trades, 1)
        self.assertGreater(result.final_value, 10_000)

    def test_audit_detects_missing_and_lagging_data(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE securities (symbol TEXT, is_active INTEGER);
            CREATE TABLE daily_kline (symbol TEXT, trade_date TEXT);
            CREATE TABLE download_failures (symbol TEXT);
            INSERT INTO securities VALUES ('600000', 1), ('000001', 1), ('300001', 1);
            INSERT INTO daily_kline VALUES ('600000', '2024-01-10'), ('000001', '2024-01-01');
        """)
        report = audit_database(conn, max_lag_days=7, coverage_threshold=0.3)
        self.assertEqual(report.no_data_symbols, 1)
        self.assertEqual(report.lagging_symbols, 1)
        self.assertFalse(report.ok)

    def test_incremental_update_reuses_latest_day_and_bootstraps_empty_db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE kline (trade_date TEXT)")
        self.assertEqual(incremental_start(conn, "2024-01-10", "2024-01-01"), "2024-01-01")
        conn.execute("INSERT INTO kline VALUES ('2024-01-09')")
        self.assertEqual(incremental_start(conn, "2024-01-10", "2024-01-01"), "2024-01-09")

    def test_report_is_reproducible_and_warns_about_small_sample(self):
        bars = [Bar(f"2024-01-{i:02d}", 10, 10) for i in range(1, 5)]
        bars += [Bar("2024-01-05", 10, 12), Bar("2024-01-06", 10, 12), Bar("2024-01-07", 11, 13)]
        result = dual_ma_backtest(bars, "600000", fast=1, slow=3, commission_rate=0, minimum_commission=0, stamp_duty_rate=0, slippage_bps=0)
        self.assertTrue(any("样本过少" in warning for warning in research_warnings(result)))
        with TemporaryDirectory() as directory:
            path = write_backtest_report(Path(directory) / "report.json", "dual_ma", {"fast": 1}, result)
            document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["strategy"], "dual_ma")
        self.assertEqual(document["result"]["symbol"], "600000")

    def test_walk_forward_keeps_training_and_validation_separate(self):
        days = [f"2024-01-{i:02d}" for i in range(1, 13)]
        panel = {
            "600000": {day: Bar(day, 10 + i, 10 + i) for i, day in enumerate(days)},
            "000001": {day: Bar(day, 10, 10) for day in days},
        }
        result = walk_forward_rotation(panel, train_ratio=0.5, top_n=1, lookback=2, rebalance_every=3, initial_cash=10_000, commission_rate=0, minimum_commission=0, stamp_duty_rate=0, slippage_bps=0)
        self.assertLess(result.train.end, result.validation.start)
        with TemporaryDirectory() as directory:
            path = write_walk_forward_report(Path(directory) / "validation.json", {"lookback": 2}, result)
            document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["result"]["split_day"], result.split_day)


if __name__ == "__main__":
    unittest.main()
