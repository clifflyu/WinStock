import unittest
import sqlite3
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from winstocker.audit import audit_database
from winstocker.cli import incremental_start, save_default_candidate_snapshot
from winstocker.candidates import momentum_candidates
from winstocker.evaluation import compare_buy_and_hold
from winstocker.gate import evaluate_gate
from winstocker.snapshots import save_snapshot, snapshot_status
from winstocker.paper import account_status, create_account, mark_account
from winstocker.robustness import walk_forward_robustness
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

    def test_rotation_excludes_insufficient_liquidity(self):
        days = [f"2024-01-{i:02d}" for i in range(1, 8)]
        panel = {"600000": {day: Bar(day, 10 + i, 10 + i, 100) for i, day in enumerate(days)}}
        result = momentum_rotation_backtest(panel, top_n=1, lookback=3, rebalance_every=10, initial_cash=10_000, commission_rate=0, minimum_commission=0, stamp_duty_rate=0, slippage_bps=0, min_history=3, min_avg_amount=1_000)
        self.assertEqual(result.trades, 0)

    def test_audit_detects_missing_and_lagging_data(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
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
        self.addCleanup(conn.close)
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

    def test_benchmark_comparison_calculates_excess_return(self):
        benchmark = [Bar("2024-01-02", 105, 105), Bar("2024-01-01", 100, 100)]
        comparison = compare_buy_and_hold("000300", benchmark, "2024-01-01", "2024-01-02", 0.12)
        self.assertAlmostEqual(comparison.benchmark_return, 0.05)
        self.assertAlmostEqual(comparison.excess_return, 0.07)

    def test_benchmark_comparison_rejects_incomplete_period(self):
        with self.assertRaises(ValueError):
            compare_buy_and_hold("000300", [Bar("2024-01-10", 100, 100), Bar("2024-01-11", 101, 101)], "2024-01-01", "2024-02-01", 0.1)

    def test_candidates_exclude_st_and_low_liquidity(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("CREATE TABLE securities (symbol TEXT, name TEXT, is_active INTEGER); CREATE TABLE daily_kline (symbol TEXT, trade_date TEXT, close REAL, amount REAL);")
        for symbol, name, amount in [("600000", "好公司", 2_000), ("000001", "*ST测试", 2_000), ("300001", "低流动", 10)]:
            conn.execute("INSERT INTO securities VALUES (?, ?, 1)", (symbol, name))
            for day, close in [("2024-01-01", 10), ("2024-01-02", 11), ("2024-01-03", 12)]:
                conn.execute("INSERT INTO daily_kline VALUES (?, ?, ?, ?)", (symbol, day, close, amount))
        picks = momentum_candidates(conn, "2024-01-03", top_n=5, lookback=2, min_history=3, min_avg_amount=1_000)
        self.assertEqual([item.symbol for item in picks], ["600000"])

    def test_research_gate_rejects_negative_holdout(self):
        class Audit: integrity, failures = "ok", 0
        class Holdout: trades, total_return, max_drawdown = 12, -0.01, -0.10
        class Validation: validation = Holdout()
        class Comparison: excess_return = 0.1
        result = evaluate_gate(Audit(), Validation(), Comparison())
        self.assertFalse(result.passed)
        self.assertIn("样本外收益非正。", result.reasons)

    def test_snapshots_are_dated_and_idempotent(self):
        from winstocker.candidates import Candidate
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        candidate = Candidate("600000", "测试", "2024-01-02", 0.1, 30_000_000, 250)
        self.assertEqual(save_snapshot(conn, "momentum-20", [candidate]), 1)
        self.assertEqual(save_snapshot(conn, "momentum-20", [candidate]), 1)
        self.assertEqual(snapshot_status(conn), (1, 1, "2024-01-02"))

    def test_default_snapshot_uses_complete_day_candidates(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("CREATE TABLE securities (symbol TEXT, name TEXT, is_active INTEGER); CREATE TABLE daily_kline (symbol TEXT, trade_date TEXT, close REAL, amount REAL); CREATE TABLE download_failures (symbol TEXT);")
        conn.execute("INSERT INTO securities VALUES ('600000', '测试', 1)")
        for number in range(250):
            conn.execute("INSERT INTO daily_kline VALUES ('600000', ?, ?, ?)", (f"2024-01-{number:03d}", 10 + number / 100, 30_000_000))
        self.assertEqual(save_default_candidate_snapshot(conn), 1)
        self.assertEqual(snapshot_status(conn)[1], 1)

    def test_paper_account_is_local_and_records_equity(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("CREATE TABLE securities (symbol TEXT, is_active INTEGER); CREATE TABLE daily_kline (symbol TEXT, trade_date TEXT, close REAL); CREATE TABLE download_failures (symbol TEXT);")
        conn.execute("INSERT INTO securities VALUES ('600000', 1)")
        conn.execute("INSERT INTO daily_kline VALUES ('600000', '2024-01-02', 10)")
        create_account(conn, "test", 100_000)
        status = mark_account(conn, "test", "2024-01-02")
        self.assertEqual(status.total_value, 100_000)
        self.assertEqual(account_status(conn, "test", "2024-01-02").positions, 0)

    def test_robustness_rejects_a_negative_validation_window(self):
        days = [f"2024-01-{i:02d}" for i in range(1, 31)]
        panel = {"600000": {day: Bar(day, 10 + (i if i < 15 else 30 - i), 10 + (i if i < 15 else 30 - i), 30_000_000) for i, day in enumerate(days)}}
        result = walk_forward_robustness(panel, ratios=(0.6,), top_n=1, lookback=2, rebalance_every=3, initial_cash=10_000, commission_rate=0, minimum_commission=0, stamp_duty_rate=0, slippage_bps=0)
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
