import sqlite3
import unittest
from unittest.mock import patch

from winstocker.enrichment import enrichment_status, update_financials, update_industries


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE securities (symbol TEXT, is_active INTEGER);
            INSERT INTO securities VALUES ('600000',1),('000001',1);
        """)

    def test_financials_preserve_notice_date_for_point_in_time_use(self):
        row = {"SECURITY_CODE":"600000", "REPORT_DATE":"2024-12-31 00:00:00",
               "NOTICE_DATE":"2025-03-28 00:00:00", "REPORT_TYPE":"年报", "ORG_TYPE":"银行",
               "ROEJQ":9.1, "TOTALOPERATEREVE":100, "TOTALOPERATEREVETZ":5,
               "PARENTNETPROFIT":20, "PARENTNETPROFITTZ":6, "NETCASH_OPERATE_PK":30,
               "ZCFZL":80, "TOTAL_ASSETS_PK":1000, "TOTAL_EQUITY_PK":200}
        with patch("winstocker.enrichment.fetch_financial_batch", return_value=[row]):
            update_financials(self.conn, workers=1)
        stored = self.conn.execute("SELECT report_date,notice_date,roe FROM financial_reports").fetchone()
        self.assertEqual(stored, ("2024-12-31", "2025-03-28", 9.1))

    def test_industry_membership_is_dated_not_backfilled(self):
        with patch("winstocker.enrichment.fetch_industry_boards", return_value=[("BK1","银行")]), \
             patch("winstocker.enrichment.fetch_board_members", return_value=["600000","900001"]):
            update_industries(self.conn, "2025-03-28", workers=1)
        self.assertEqual(self.conn.execute("SELECT as_of,board_code,symbol FROM industry_memberships").fetchone(),
                         ("2025-03-28", "BK1", "600000"))
        status = enrichment_status(self.conn)
        self.assertEqual(status.industry_dates, 1)


if __name__ == "__main__":
    unittest.main()
