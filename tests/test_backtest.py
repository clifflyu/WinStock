import unittest
import sqlite3
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from winstocker.audit import audit_database, scale_drift
from winstocker.calendar import refresh_calendar, suspended_days, trading_days_between
from winstocker.cli import save_default_candidate_snapshot, symbol_starts
from winstocker.candidates import Candidate, momentum_candidates
from winstocker.evaluation import compare_buy_and_hold
from winstocker.gate import evaluate_gate
from winstocker.snapshots import DEFAULT_STRATEGY_KEY, previous_snapshot, save_snapshot, snapshot_status
from winstocker.paper import (account_status, create_account, enable_auto_strategy, mark_account,
                              rebalance_account)
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

    def test_symbol_starts_are_per_symbol_and_repair_refetches_full_history(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE securities (id INTEGER PRIMARY KEY, symbol TEXT);
            CREATE TABLE kline (symbol_id INTEGER, trade_date TEXT);
            INSERT INTO securities VALUES (1, '600000'), (2, '000001'), (3, '300001');
            INSERT INTO kline VALUES (1, '2024-01-02'), (1, '2026-09-15'), (1, '2026-09-16');
            INSERT INTO kline VALUES (2, '2024-01-02'), (2, '2024-01-03');
        """)
        # 全库 MAX 会让 000001 的缺口永远补不上；按股各自的末日才是对的。
        starts = symbol_starts(conn, "2026-09-17", "2024-01-01", set())
        self.assertEqual(starts["600000"], "2026-09-16")
        self.assertEqual(starts["000001"], "2024-01-03")
        # 尺度漂移的股票从自己的首日重抓全史，而不是只补最新几天。
        starts = symbol_starts(conn, "2026-09-17", "2024-01-01", {"000001"})
        self.assertEqual(starts["000001"], "2024-01-02")
        # 完全没有数据的股票不在映射里，调用方回退到 bootstrap_start。
        self.assertNotIn("300001", starts)

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
        # 替身必须与 DataAudit 的接口一致：gate 现在消费真实缺陷计数，
        # 而不只是 integrity/failures。
        class Audit: integrity, failures, no_data_failed, lagging_failed, broken_change_rows = "ok", 0, 0, 0, 0
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

    def test_previous_snapshot_is_strictly_earlier_than_data_date(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        earlier = Candidate("600000", "前日", "2024-01-02", 0.1, 30_000_000, 250)
        current = Candidate("000001", "当日", "2024-01-03", 0.2, 40_000_000, 250)
        save_snapshot(conn, DEFAULT_STRATEGY_KEY, [earlier])
        save_snapshot(conn, DEFAULT_STRATEGY_KEY, [current])
        as_of, symbols = previous_snapshot(conn, "2024-01-03")
        self.assertEqual(as_of, "2024-01-02")
        self.assertEqual(symbols, ("600000",))

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

    def test_auto_paper_rebalance_uses_previous_snapshot_and_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE daily_kline
              (symbol TEXT, trade_date TEXT, open REAL, close REAL, amount REAL);
            CREATE TABLE trading_calendar (trade_date TEXT PRIMARY KEY);
            INSERT INTO trading_calendar VALUES ('2024-01-02'), ('2024-01-03');
            INSERT INTO daily_kline VALUES
              ('600000', '2024-01-02', 10, 10, 30000000),
              ('600000', '2024-01-03', 10.1, 10.2, 30000000),
              ('000001', '2024-01-02', 20, 20, 30000000),
              ('000001', '2024-01-03', 20.1, 20.2, 30000000);
        """)
        save_snapshot(conn, DEFAULT_STRATEGY_KEY, [
            Candidate("600000", "甲", "2024-01-02", 0.2, 30_000_000, 250),
            Candidate("000001", "乙", "2024-01-02", 0.1, 30_000_000, 250),
        ])
        create_account(conn, "auto", 10_000)
        enable_auto_strategy(conn, "auto", top_n=2, rebalance_every=20)
        first = rebalance_account(conn, "auto", "2024-01-03")
        self.assertTrue(first.executed)
        self.assertEqual(first.buys, 2)
        trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        cash = first.status.cash
        second = rebalance_account(conn, "auto", "2024-01-03")
        self.assertFalse(second.executed)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], trades)
        self.assertEqual(second.status.cash, cash)

    def test_dual_ma_counts_only_suspension_days_actually_held(self):
        # slow=2 时首个信号出现在索引 3，因此停牌要放在那之后才可能被持仓覆盖。
        calendar = [f"2024-01-{i:02d}" for i in range(1, 9)]
        rising = {1: 10, 2: 11, 3: 12, 4: 13, 7: 14, 8: 15}

        def bars_for(present):
            return [Bar(f"2024-01-{i:02d}", p, p) for i, p in sorted(rising.items()) if i in present]

        def run(present):
            return dual_ma_backtest(bars_for(present), "600000", fast=1, slow=2, commission_rate=0,
                                    minimum_commission=0, stamp_duty_rate=0, slippage_bps=0,
                                    trading_days=calendar)

        # 停牌（01-05、01-06）落在建仓之后：仓位被锁死，两天都计入暴露。
        self.assertEqual(run({1, 2, 3, 4, 7, 8}).suspension_days, 2)
        # 同样两天停牌，但建仓还没发生：空仓期间没有卖出风险，不应计入。
        self.assertEqual(run({1, 2, 3, 5, 6, 7, 8}).suspension_days, 0)
        # 不传日历时无法判断缺失日是不是交易日，只能保守报 0。
        self.assertEqual(dual_ma_backtest(bars_for({1, 2, 3, 5, 6, 7, 8}), "600000",
                                          fast=1, slow=2).suspension_days, 0)

    def test_backtest_momentum_window_uses_lookback_intervals(self):
        """回测的动量窗口必须与 candidates 同口径：止于昨日、跨度 lookback 个交易日区间。

        这里两支股票的强弱会随窗口长度反转——600000 在更长的窗口里更强，000001 在更短的
        窗口里更强——所以口径不同会选出不同标的。按 lookback-1 个区间（旧口径）算，01-05
        那次调仓会从 600000 换成 000001（多出一次卖出+买入 = 3 笔）；按 lookback 个区间算，
        600000 始终最强，全程只成交 1 笔。
        """
        days = [f"2024-01-0{i}" for i in range(1, 6)]
        def series(closes, last_open):
            return {d: Bar(d, last_open if index == 4 else close, close)
                    for index, (d, close) in enumerate(zip(days, closes))}
        panel = {
            "600000": series([10, 10, 10.5, 11, 11.5], 11.2),
            "000001": series([10, 10, 10, 10.6, 10.7], 10.7),
        }
        result = momentum_rotation_backtest(
            panel, top_n=1, lookback=2, rebalance_every=1, initial_cash=10_000,
            commission_rate=0, minimum_commission=0, stamp_duty_rate=0, slippage_bps=0,
        )
        self.assertEqual(result.trades, 1)
        self.assertAlmostEqual(result.final_value, 10_450)

    def test_momentum_requires_lookback_plus_two_days(self):
        # 窗口要 lookback+1 根（基准日到昨日），再加上当日开盘成交的那一天。
        def run(count, lookback):
            days = [f"2024-01-{i:02d}" for i in range(1, count + 1)]
            return momentum_rotation_backtest({"600000": {d: Bar(d, 10, 10) for d in days}},
                                              top_n=1, lookback=lookback, rebalance_every=1)
        with self.assertRaises(ValueError):
            run(3, 2)   # 3 根只够 lookback+1，旧口径会错误地放行
        run(4, 2)

    def test_scale_drift_detector_finds_a_scale_boundary(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE securities (id INTEGER PRIMARY KEY, symbol TEXT);
            CREATE TABLE kline (symbol_id INTEGER, trade_date TEXT, close INTEGER, change_amount INTEGER);
            INSERT INTO securities VALUES (1, '600000'), (2, '000001');
            -- 600000 自洽：涨跌额恒等于相邻收盘价之差
            INSERT INTO kline VALUES (1, '2024-01-02', 10000, NULL),
                                     (1, '2024-01-03', 10010, 10),
                                     (1, '2024-01-04', 10020, 10);
            -- 000001 在 01-04 越过尺度边界：实际差了 -260，但涨跌额仍记为 10
            INSERT INTO kline VALUES (2, '2024-01-02', 20000, NULL),
                                     (2, '2024-01-03', 20010, 10),
                                     (2, '2024-01-04', 19750, 10);
        """)
        self.assertEqual(scale_drift(conn), [("000001", "2024-01-04")])

    def test_scale_drift_degrades_when_schema_is_minimal(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE daily_kline (symbol TEXT, trade_date TEXT)")
        self.assertEqual(scale_drift(conn), [])  # 没有 kline 表时不应抛异常

    def test_calendar_derives_suspensions_and_skips_partial_days(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE securities (id INTEGER PRIMARY KEY, symbol TEXT, is_active INTEGER);
            CREATE TABLE kline (symbol_id INTEGER, trade_date TEXT, PRIMARY KEY (symbol_id, trade_date));
            CREATE VIEW daily_kline AS
                SELECT s.symbol AS symbol, k.trade_date AS trade_date
                FROM kline k JOIN securities s ON s.id = k.symbol_id;
            CREATE TABLE download_failures (symbol TEXT);
            INSERT INTO securities VALUES (1,'600000',1), (2,'000001',1), (3,'300001',1);
            INSERT INTO kline VALUES (1,'2024-01-02'),(1,'2024-01-03'),(1,'2024-01-04'),(1,'2024-01-05');
            -- 000001 在 01-04 停牌
            INSERT INTO kline VALUES (2,'2024-01-02'),(2,'2024-01-03'),(2,'2024-01-05');
            INSERT INTO kline VALUES (3,'2024-01-02'),(3,'2024-01-03'),(3,'2024-01-04'),(3,'2024-01-05');
            -- 01-06 只有 1/3 的股票有行情：覆盖率不足，不算交易日
            INSERT INTO kline VALUES (1,'2024-01-06');
        """)
        # 生产阈值 90% 在只有 3 只股票时会让 01-04（2/3）也落选，
        # 因此这里调低阈值，专注验证「推导停牌」与「排除残缺日」两件事。
        days, suspended = refresh_calendar(conn, coverage_threshold=0.6)
        self.assertEqual(days, 4)
        self.assertNotIn("2024-01-06", trading_days_between(conn, None, None))
        self.assertEqual(suspended, 1)
        self.assertEqual(suspended_days(conn, "000001", "2024-01-01", "2024-01-31"), {"2024-01-04"})
        self.assertEqual(suspended_days(conn, "600000", "2024-01-01", "2024-01-31"), set())

    def test_fetch_failures_are_not_recorded_as_suspensions(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE securities (id INTEGER PRIMARY KEY, symbol TEXT, is_active INTEGER);
            CREATE TABLE kline (symbol_id INTEGER, trade_date TEXT, PRIMARY KEY (symbol_id, trade_date));
            CREATE VIEW daily_kline AS
                SELECT s.symbol AS symbol, k.trade_date AS trade_date
                FROM kline k JOIN securities s ON s.id = k.symbol_id;
            CREATE TABLE download_failures (symbol TEXT);
            INSERT INTO securities VALUES (1,'600000',1), (2,'000001',1), (3,'300001',1);
            INSERT INTO kline VALUES (1,'2024-01-02'),(1,'2024-01-03'),(1,'2024-01-04'),(1,'2024-01-05');
            INSERT INTO kline VALUES (2,'2024-01-02'),(2,'2024-01-03'),(2,'2024-01-05');
            INSERT INTO kline VALUES (3,'2024-01-02'),(3,'2024-01-03'),(3,'2024-01-04'),(3,'2024-01-05');
            INSERT INTO download_failures VALUES ('000001');
        """)
        # 抓取失败的股票缺口可能只是漏抓，不能当成停牌豁免掉。
        _, suspended = refresh_calendar(conn)
        self.assertEqual(suspended, 0)

    def test_suspended_holding_is_sold_at_resumption_open(self):
        days = [f"2024-01-{i:02d}" for i in range(1, 9)]
        # 单日幅度必须留在涨跌停内，否则会被涨停拦截而根本买不进（那才是对的）。
        rising = {"2024-01-01": 10, "2024-01-02": 10.5, "2024-01-03": 11, "2024-01-04": 11.5, "2024-01-07": 11.4, "2024-01-08": 11.4}
        panel = {
            # 600000 在 01-05、01-06 停牌，01-07 复牌
            "600000": {d: Bar(d, p, p) for d, p in rising.items()},
            # 000001 必须全程交易：若两只同时停牌，那两个交易日会从并集里整个消失，
            # 回测根本看不到它们（这正是「停牌对回测不可见」的机制之一）。
            "000001": {d: Bar(d, 10, 10) for d in days},
        }
        result = momentum_rotation_backtest(
            panel, top_n=1, lookback=2, rebalance_every=1, initial_cash=100_000,
            commission_rate=0, minimum_commission=0, stamp_duty_rate=0, slippage_bps=0,
        )
        # 停牌期间挂起，而不是当作没发生；复牌当日按真实开盘价退出。
        self.assertEqual(result.suspension_blocked_sells, 1)
        self.assertEqual(result.suspension_days, 2)
        self.assertEqual(result.trades, 3)  # 买入 600000、买入 000001、复牌卖出 600000

    def test_gate_rejects_scale_drift_even_when_holdout_passes(self):
        class Audit:
            integrity, failures, no_data_failed, lagging_failed, broken_change_rows = "ok", 0, 0, 0, 24
        class Holdout: trades, total_return, max_drawdown = 12, 0.30, -0.05
        class Validation: validation = Holdout()
        class Comparison: excess_return = 0.1
        result = evaluate_gate(Audit(), Validation(), Comparison())
        self.assertFalse(result.passed)
        self.assertTrue(any("尺度异常" in reason for reason in result.reasons))

    def test_robustness_rejects_a_negative_validation_window(self):
        days = [f"2024-01-{i:02d}" for i in range(1, 31)]
        panel = {"600000": {day: Bar(day, 10 + (i if i < 15 else 30 - i), 10 + (i if i < 15 else 30 - i), 30_000_000) for i, day in enumerate(days)}}
        result = walk_forward_robustness(panel, ratios=(0.6,), top_n=1, lookback=2, rebalance_every=3, initial_cash=10_000, commission_rate=0, minimum_commission=0, stamp_duty_rate=0, slippage_bps=0)
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
