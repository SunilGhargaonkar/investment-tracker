"""Hourly profit/loss per holding (dashboard.hourly_changes). Offline, temp DB only."""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import dashboard  # noqa: E402

H = 3600


def ts(day, hour, minute=0):
    return int(time.mktime(time.strptime(f"{day} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M")))


class HourlyTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "t.db")
        self.c = sqlite3.connect(self.db)
        self.c.execute("CREATE TABLE snaps(ts INTEGER, account TEXT, ticker TEXT, qty REAL,"
                       " price REAL, ppl REAL, avg REAL)")

    def snap(self, t, ticker, qty, price, ppl, acct="ISA"):
        self.c.execute("INSERT INTO snaps VALUES (?,?,?,?,?,?,NULL)", (t, acct, ticker, qty, price, ppl))
        self.c.commit()

    def run_hourly(self, first_day="2026-11-10"):
        dashboard.refresh_hourly(self.c)
        return dashboard.hourly_changes(self.c, first_day)

    def test_each_hour_is_change_since_end_of_previous_hour(self):
        self.snap(ts("2026-11-09", 23, 50), "NVDA_US_EQ", 2, 100, 10.0)  # baseline from the day before
        self.snap(ts("2026-11-10", 0, 10), "NVDA_US_EQ", 2, 101, 11.0)
        self.snap(ts("2026-11-10", 0, 50), "NVDA_US_EQ", 2, 103, 14.0)   # last in hour 0 counts
        self.snap(ts("2026-11-10", 1, 30), "NVDA_US_EQ", 2, 99, 6.5)
        cell = self.run_hourly()["2026-11-10"][("ISA", "NVDA")]
        self.assertAlmostEqual(cell["v"][0], 4.0)
        self.assertAlmostEqual(cell["v"][1], -7.5)
        self.assertEqual(cell["e"], [])
        self.assertEqual(cell["g"], {})

    def test_day_total_matches_ppl_change(self):
        ppls = [5.0, 7.0, 4.0, 9.5, 8.0]
        self.snap(ts("2026-11-09", 22), "AAPL_US_EQ", 1, 200, 3.0)
        for i, p in enumerate(ppls):
            self.snap(ts("2026-11-10", 9 + i, 30), "AAPL_US_EQ", 1, 200, p)
        cell = self.run_hourly()["2026-11-10"][("ISA", "AAPL")]
        self.assertAlmostEqual(sum(cell["v"].values()), ppls[-1] - 3.0)

    def test_quantity_change_is_estimated_from_price_move(self):
        self.snap(ts("2026-11-10", 14, 30), "VUAGl_EQ", 10, 90.0, 50.0)
        self.snap(ts("2026-11-10", 15, 30), "VUAGl_EQ", 15, 91.0, 55.0)  # bought 5 more
        cell = self.run_hourly()["2026-11-10"][("ISA", "VUAGl")]
        self.assertAlmostEqual(cell["v"][15], (91.0 - 90.0) * 15)  # GBP line, no FX
        self.assertEqual(cell["e"], [15])

    def test_us_estimate_converted_to_gbp(self):
        self.snap(ts("2026-11-10", 14, 30), "AMD_US_EQ", 1, 100.0, 0.0)
        self.snap(ts("2026-11-10", 15, 30), "AMD_US_EQ", 2, 110.0, 0.0)
        cell = self.run_hourly()["2026-11-10"][("ISA", "AMD")]
        self.assertAlmostEqual(cell["v"][15], 10.0 * 2 * dashboard.FX_GBP_PER_USD)

    def test_gap_is_flagged_and_covers_the_gap(self):
        self.snap(ts("2026-11-10", 9, 30), "MSFT_US_EQ", 1, 400, 1.0)
        self.snap(ts("2026-11-10", 13, 15), "MSFT_US_EQ", 1, 410, 8.0)  # tracker off 10:00-13:00
        cell = self.run_hourly()["2026-11-10"][("ISA", "MSFT")]
        self.assertAlmostEqual(cell["v"][13], 7.0)
        self.assertIn(13, cell["g"])

    def test_alias_merges_old_code_and_accounts_stay_separate(self):
        for t in ("YNDX_US_EQ", "NBIS_US_EQ"):
            self.snap(ts("2026-11-10", 10, 30), t, 1, 50, 0.0)
            self.snap(ts("2026-11-10", 11, 30), t, 1, 51, 1.0)
        self.snap(ts("2026-11-10", 10, 30), "NBIS_US_EQ", 1, 50, 0.0, acct="INVEST")
        self.snap(ts("2026-11-10", 11, 30), "NBIS_US_EQ", 1, 52, 3.0, acct="INVEST")
        day = self.run_hourly()["2026-11-10"]
        self.assertAlmostEqual(day[("ISA", "NBIS")]["v"][11], 2.0)
        self.assertAlmostEqual(day[("INVEST", "NBIS")]["v"][11], 3.0)

    def test_incremental_refresh_matches_full_rebuild(self):
        t0 = ts("2026-11-10", 8)
        for i in range(30):  # every 10 minutes for 5 hours, refreshing as we go
            self.snap(t0 + i * 600, "AMZN_US_EQ", 1, 200 + i, float(i * i % 7))
            if i % 4 == 0:
                dashboard.refresh_hourly(self.c)
        incremental = self.run_hourly()
        self.c.execute("DELETE FROM snaps_hourly")
        self.assertEqual(self.run_hourly(), incremental)

    def test_page_includes_hourly_section(self):
        self.snap(ts("2026-11-10", 10, 30), "NVDA_US_EQ", 1, 100, 1.0)
        self.snap(ts("2026-11-10", 11, 30), "NVDA_US_EQ", 1, 101, 2.0)
        out = os.path.join(os.path.dirname(self.db), "d.html")
        self.c.close()
        dashboard.build(self.db, out)
        with open(out, encoding="utf-8") as f:
            page = f.read()
        self.assertIn('id="hourly"', page)
        self.assertIn('"2026-11-10"', page)
        self.assertNotIn('http-equiv="refresh"', page)  # a meta refresh would drop the chosen date


if __name__ == "__main__":
    unittest.main()
