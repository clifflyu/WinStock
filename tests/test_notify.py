import base64
import hashlib
import hmac
import json
import shutil
import sqlite3
import subprocess
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from winstocker.audit import DataAudit
from winstocker.candidates import Candidate
from winstocker.cli import DigestOptions, build_digest
from winstocker.notify import (DOTENV_PATH, ERROR_MAX_CHARS, FEISHU_MAX_BODY, SECRET_ENV, WEBHOOK_ENV,
                               DailyDigest, build_card, build_payload, build_test_card, credential,
                               dotenv_values, feishu_signature, format_amount, format_momentum,
                               parse_dotenv, require_credential)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

NOW = datetime(2026, 9, 17, 19, 35, 12)


def healthy_audit(**overrides):
    base = dict(integrity="ok", active_securities=5206, symbols_with_bars=5200, rows=3_349_424,
                first_day="2024-01-02", latest_day="2026-09-17", latest_day_symbols=5206,
                newest_observed_day="2026-09-17", newest_observed_symbols=5206,
                no_data_symbols=0, lagging_symbols=0, failures=0)
    base.update(overrides)
    return DataAudit(**base)


def digest(**overrides):
    """一个状态正常的摘要，测试按需覆盖字段。"""
    base = dict(generated_at=NOW, data_date="2026-09-17", update_error=None, data_error=None,
                audit=healthy_audit(), candidates=(), lookback=20, min_history=250,
                min_avg_amount=20_000_000)
    base.update(overrides)
    return DailyDigest(**base)


class NotifyTests(unittest.TestCase):
    def test_signature_uses_timestamp_newline_secret_as_key(self):
        secret, timestamp = "s3cret", 1758108912
        expected = base64.b64encode(
            hmac.new(f"{timestamp}\n{secret}".encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        self.assertEqual(feishu_signature(secret, timestamp), expected)
        # 光断言等于预期值还不够：这个测试真正的价值是钉死「key 是拼接串、message 为空」
        # 这个反直觉的顺序。把直觉上更合理的写法（secret 当 key）排除掉，否则这条测试
        # 会在有人「顺手改顺」时静默通过，直到真实发送才以 19021 暴露。
        swapped = base64.b64encode(
            hmac.new(secret.encode("utf-8"), f"{timestamp}\n".encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        self.assertNotEqual(feishu_signature(secret, timestamp), swapped)

    def test_signature_absent_when_no_secret_configured(self):
        card = build_card(digest())
        self.assertNotIn("sign", build_payload(card))
        self.assertNotIn("timestamp", build_payload(card))
        payload = build_payload(card, secret="s", timestamp=1758108912)
        self.assertEqual(payload["timestamp"], "1758108912")
        self.assertIn("sign", payload)
        # 时间戳必须是秒级十进制字符串：毫秒或整数都会验签失败。
        self.assertRegex(payload["timestamp"], r"^\d{10}$")

    def test_failure_card_is_red_and_still_carries_the_pool(self):
        card = build_card(digest(
            update_error="请求失败: 行情接口未返回 data",
            candidates=(Candidate("600519", "贵州茅台", "2026-09-17", 0.123, 8.32e8, 300),),
            data_date="2026-09-16",
        ))
        self.assertEqual(card["header"]["template"], "red")
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("请求失败", rendered)
        self.assertIn("600519", rendered)
        # 失败时标题仍用数据日期，用户才知道手上这份数据是哪天的。
        self.assertIn("2026-09-16", rendered)
        # 钉死 v1 结构：v2 要求客户端 7.20+，低版本只显示标题加一句升级提示。
        self.assertNotIn("schema", card)
        self.assertIn("elements", card)

    def test_momentum_and_amount_formatting(self):
        self.assertEqual(format_momentum(0.1234), "+12.34%")
        self.assertEqual(format_momentum(-0.045), "-4.50%")
        self.assertEqual(format_momentum(0.0), "+0.00%")
        self.assertEqual(format_amount(8.32e8), "8.32 亿")
        self.assertEqual(format_amount(2.35e7), "2350 万")

    def test_empty_pool_is_not_a_failure(self):
        card = build_card(digest(candidates=()))
        self.assertEqual(card["header"]["template"], "green")
        self.assertEqual(digest(candidates=()).status, "ok")
        # 必须明说这是正常的，否则非技术用户看到空列表会以为系统坏了。
        self.assertIn("正常结果", json.dumps(card, ensure_ascii=False))

    def test_payload_stays_under_feishu_body_limit(self):
        # 最坏情况：错误文本已按上限截断，候选又都是长名字。
        worst = digest(
            update_error="异" * ERROR_MAX_CHARS,
            candidates=tuple(
                Candidate(f"6005{i:02d}", "某只名字相当长的股票" * 2, "2026-09-17", 0.9 - i * 0.01, 8.32e8, 300)
                for i in range(10)
            ),
        )
        payload = build_payload(build_card(worst), secret="s", timestamp=1758108912)
        # 必须按 UTF-8 字节数算：中文一个字符 3 字节，用 len(str) 会低估三倍。
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.assertLess(size, FEISHU_MAX_BODY)
        self.assertEqual(len(worst.update_error), ERROR_MAX_CHARS)

    def test_credential_prefers_flag_over_env(self):
        # 一律显式传空的 dotenv：默认值会去读项目根目录那个真实的 .env，让测试结果
        # 取决于开发机上有没有配置文件——同一份代码在不同机器上时红时绿。
        env, empty = {WEBHOOK_ENV: "https://env"}, {}
        self.assertEqual(credential("https://flag", WEBHOOK_ENV, env, empty), "https://flag")
        self.assertEqual(credential(None, WEBHOOK_ENV, env, empty), "https://env")
        self.assertIsNone(credential(None, WEBHOOK_ENV, empty, empty))
        # 空串与纯空白都不算「已配置」，否则配置文件里留空的键会被当成有效值。
        self.assertIsNone(credential(None, SECRET_ENV, {SECRET_ENV: "  "}, empty))
        # 空 flag 等同于没传，继续回退。
        self.assertEqual(credential("", WEBHOOK_ENV, env, empty), "https://env")
        self.assertIsNone(credential("   ", WEBHOOK_ENV, {WEBHOOK_ENV: "  "}, empty))
        with self.assertRaises(RuntimeError):
            require_credential(None, WEBHOOK_ENV, "飞书 Webhook 地址", empty, empty)

    def test_dotenv_parsing(self):
        parsed = parse_dotenv(
            "# 注释行\n"
            "\n"
            "   \n"
            "WINSTOCK_FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/abc\n"
            "export WINSTOCK_FEISHU_SECRET='quoted'\n"
            "WINSTOCK_FEISHU_SECRET_DQ=\"double\"\n"
            "  空格键 =  值两边有空格  \n"
            "没有等号的行\n"
            "EMPTY=\n"
        )
        self.assertEqual(parsed["WINSTOCK_FEISHU_WEBHOOK"],
                         "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
        self.assertEqual(parsed["WINSTOCK_FEISHU_SECRET"], "quoted")
        self.assertEqual(parsed["WINSTOCK_FEISHU_SECRET_DQ"], "double")
        self.assertEqual(parsed["空格键"], "值两边有空格")
        self.assertEqual(parsed["EMPTY"], "")
        self.assertNotIn("没有等号的行", parsed)

    def test_credential_falls_back_to_dotenv(self):
        dotenv = {WEBHOOK_ENV: "https://dotenv", SECRET_ENV: "s"}
        # 优先级：命令行参数 > 环境变量 > .env
        self.assertEqual(credential("https://flag", WEBHOOK_ENV, {WEBHOOK_ENV: "https://env"}, dotenv),
                         "https://flag")
        self.assertEqual(credential(None, WEBHOOK_ENV, {WEBHOOK_ENV: "https://env"}, dotenv),
                         "https://env")
        self.assertEqual(credential(None, WEBHOOK_ENV, {}, dotenv), "https://dotenv")
        # .env 里键存在但值为空，等同于未配置。
        self.assertIsNone(credential(None, WEBHOOK_ENV, {}, {WEBHOOK_ENV: "  "}))

    def test_dotenv_values_reads_file_and_survives_missing_one(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("A=1\n# 注释\nB='2'\n", encoding="utf-8")
            self.assertEqual(dotenv_values(path), {"A": "1", "B": "2"})
            # 文件不存在不能抛异常：配置缺失由 require_credential 给出可读提示。
            self.assertEqual(dotenv_values(Path(directory) / "没有这个文件"), {})

    def test_dotenv_lives_at_project_root(self):
        # 路径必须相对包定位，否则从别的目录调用时会去读 cwd 下的 .env。
        self.assertEqual(DOTENV_PATH, PROJECT_ROOT / ".env")

    @unittest.skipIf(shutil.which("git") is None, "需要 git")
    def test_env_file_must_stay_gitignored(self):
        """本仓库是公开的：.env 一旦被提交，Webhook 就永久留在 git 历史里。

        这条测试直接问 git 本身，而不是读 .gitignore 文本——`env/` 这种写法看着像
        覆盖了 .env 其实只匹配同名目录，只有真正的匹配引擎才作数。
        """
        result = subprocess.run(["git", "-C", str(PROJECT_ROOT), "check-ignore", "-q", ".env"],
                                capture_output=True, text=True)
        if result.returncode not in (0, 1):
            self.skipTest("当前目录不是 git 仓库")
        self.assertEqual(result.returncode, 0, ".env 未被 .gitignore 忽略，提交它会泄漏飞书 Webhook")

    def test_test_card_needs_no_database(self):
        card = build_test_card(NOW, secret_configured=False)
        self.assertEqual(card["header"]["template"], "blue")
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("2026-09-17 19:35:12", rendered)
        self.assertIn("未开启", rendered)

    def test_build_digest_reads_candidates_and_stays_green(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        days = [(date(2024, 1, 1) + timedelta(days=index)).isoformat() for index in range(300)]
        conn.executescript("""
            CREATE TABLE securities (symbol TEXT, name TEXT, is_active INTEGER);
            CREATE TABLE daily_kline (symbol TEXT, trade_date TEXT, close REAL, amount REAL);
            CREATE TABLE download_failures (symbol TEXT);
            INSERT INTO securities VALUES ('600519', '贵州茅台', 1), ('000001', '平安银行', 1);
        """)
        rows = []
        for index, day in enumerate(days):
            rows.append(("600519", day, 10 + index * 0.1, 1e8))   # 稳步上涨
            rows.append(("000001", day, 10.0, 1e8))               # 横盘
        conn.executemany("INSERT INTO daily_kline VALUES (?, ?, ?, ?)", rows)

        # 刻意不建 trading_calendar：_momentum_window 会降级跳过连续性检查，这条降级路径也要覆盖。
        options = DigestOptions(top_n=2, min_history=250, min_avg_amount=1e6)
        result = build_digest(conn, options, None, 1234, NOW)

        self.assertEqual(result.data_date, days[-1])
        self.assertEqual(result.rows_added, 1234)
        self.assertEqual([item.symbol for item in result.candidates], ["600519", "000001"])
        self.assertGreater(result.candidates[0].momentum, result.candidates[1].momentum)
        self.assertEqual(result.status, "ok")
        self.assertEqual(build_card(result)["header"]["template"], "green")
        self.assertIn("1,234", json.dumps(build_card(result), ensure_ascii=False))

    def test_build_digest_degrades_instead_of_raising(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        # 空库：没有 securities 表，momentum_candidates 必然抛错。
        result = build_digest(conn, DigestOptions(), None, None, NOW)
        self.assertIsNotNone(result.data_error)
        self.assertIsNone(result.audit)
        self.assertEqual(result.status, "error")
        # 关键契约：即使读库失败，卡片也构造得出来且候选池段落仍在。
        card = build_card(result)
        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("无法读取本地数据库", json.dumps(card, ensure_ascii=False))

    def test_audit_defect_turns_card_orange(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        days = [(date(2024, 1, 1) + timedelta(days=index)).isoformat() for index in range(10)]
        conn.executescript("""
            CREATE TABLE securities (symbol TEXT, name TEXT, is_active INTEGER);
            CREATE TABLE daily_kline (symbol TEXT, trade_date TEXT, close REAL, amount REAL);
            CREATE TABLE download_failures (symbol TEXT);
            INSERT INTO securities VALUES ('600519', '贵州茅台', 1), ('000001', '平安银行', 1);
            INSERT INTO download_failures VALUES ('000001');
        """)
        conn.executemany("INSERT INTO daily_kline VALUES (?, ?, ?, ?)",
                         [(symbol, day, 10.0, 1e8) for day in days for symbol in ("600519", "000001")])
        # min_history 必须 ≥ lookback + 1，否则 momentum_candidates 直接抛参数错误，
        # 那样测到的就是红色失败分支，而不是这里要验证的橙色审计告警分支。
        result = build_digest(conn, DigestOptions(top_n=2, lookback=3, min_history=4, min_avg_amount=0),
                              None, None, NOW)
        self.assertIsNone(result.data_error)
        self.assertFalse(result.audit.ok)
        self.assertEqual(result.status, "warn")
        self.assertEqual(build_card(result)["header"]["template"], "orange")
        self.assertIn("需检查", json.dumps(build_card(result), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
