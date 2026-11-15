"""Step 2: analytics (XIRR, TWR, Section 104, tax-year boundary, dividends, benchmark,
risk, concentration, report output). Offline only, in-memory SQLite, no network."""
import csv
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import analytics  # noqa: E402
import history  # noqa: E402

D = dt.date


def new_db():
    return history.init_schema(sqlite3.connect(":memory:"))


def add_fill(c, acct, fid, ticker, side, date, qty, net, realised=0.0, fees=0.0, wallet="GBP"):
    taxes = [{"name": "FEE", "quantity": fees, "currency": wallet}] if fees else []
    raw = json.dumps({"walletImpact": {"currency": wallet, "netValue": net,
                                       "realisedProfitLoss": realised, "taxes": taxes}})
    c.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (acct, str(fid), None, ticker, ticker, None, side, "TRADE", "TOTV",
               f"{date}T10:00:00Z", net / qty, qty, "GBP", wallet, net, realised, fees, "api", raw))


def add_cash(c, acct, ref, date, typ, amount):
    c.execute("INSERT INTO cash_tx VALUES (?,?,?,?,?,?,?,?)",
              (acct, ref, f"{date}T10:00:00Z", typ, amount, "GBP", "api", "{}"))


def add_summary(c, acct, local_dt, total):
    c.execute("INSERT INTO summary_snapshots VALUES (?,?,?)",
              (int(local_dt.timestamp()), acct, json.dumps({"totalValue": total})))


def add_position(c, acct, ticker, value, ppl=0.0, cost=None):
    c.execute("INSERT INTO positions_latest VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (acct, 0, ticker, ticker, "GBP", 1, 1, ppl, value, cost, None))


def add_dividend(c, acct, ref, ticker, date, amount, dtype="ORDINARY"):
    c.execute("INSERT INTO dividends VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (acct, ref, ticker, f"{date}T00:00:00Z", 5, amount, None, None, "GBP", dtype, "api", "{}"))


class XirrTests(unittest.TestCase):
    def test_one_year_ten_percent(self):
        flows = [(D(2024, 1, 1), -1000.0), (D(2025, 1, 1), 1100.0)]  # 2024 is leap: 366 days
        r = analytics.xirr(flows)
        self.assertAlmostEqual(r, 1100 / 1000 - 1, delta=0.002)

    def test_exact_365_days(self):
        flows = [(D(2023, 1, 1), -1000.0), (D(2024, 1, 1), 1100.0)]  # 365 days
        self.assertAlmostEqual(analytics.xirr(flows), 0.10, places=6)

    def test_two_years_known_example(self):
        start, end = D(2023, 1, 1), D(2025, 1, 1)  # 731 days (includes 2024 leap day)
        flows = [(start, -1000.0), (end, 1210.0)]
        expected = 1.21 ** (365 / (end - start).days) - 1
        self.assertAlmostEqual(analytics.xirr(flows), expected, places=6)

    def test_no_sign_change_returns_none(self):
        self.assertIsNone(analytics.xirr([(D(2024, 1, 1), -1000.0), (D(2025, 1, 1), -10.0)]))


class TaxYearTests(unittest.TestCase):
    def test_boundary_5_and_6_april(self):
        self.assertEqual(analytics.tax_year(D(2025, 4, 5)), "2024/25")
        self.assertEqual(analytics.tax_year(D(2025, 4, 6)), "2025/26")
        self.assertEqual(analytics.tax_year(D(2026, 4, 5)), "2025/26")
        self.assertEqual(analytics.tax_year(D(2026, 4, 6)), "2026/27")
        self.assertEqual(analytics.tax_year(D(2025, 1, 1)), "2024/25")


class Section104Tests(unittest.TestCase):
    def test_hand_checked_example(self):
        # buys: 10 @ 100 (May), 10 @ 150 (Jun 1); pool avg 125 for 20 shares
        # Jun 10 sell 5 for 900: 5 bought on Jun 20 (inside 30 days) at 120 -> cost 600, gain 300
        # Jul 1 buy 10 @ 120 and sell 10 for 1500 the same day -> cost 1200, gain 300
        # Aug 1 sell 8 for 1200: pool avg 125 -> cost 1000, gain 200
        trades = [
            {"date": D(2024, 5, 1), "side": "BUY", "qty": 10, "value": 1000.0, "id": "b1"},
            {"date": D(2024, 6, 1), "side": "BUY", "qty": 10, "value": 1500.0, "id": "b2"},
            {"date": D(2024, 6, 10), "side": "SELL", "qty": 5, "value": 900.0, "id": "s1"},
            {"date": D(2024, 6, 20), "side": "BUY", "qty": 5, "value": 600.0, "id": "b3"},
            {"date": D(2024, 7, 1), "side": "BUY", "qty": 10, "value": 1200.0, "id": "b4"},
            {"date": D(2024, 7, 1), "side": "SELL", "qty": 10, "value": 1500.0, "id": "s2"},
            {"date": D(2024, 8, 1), "side": "SELL", "qty": 8, "value": 1200.0, "id": "s3"},
        ]
        ds, warn = analytics.s104(trades)
        self.assertEqual(warn, [])
        by = {d["id"]: d for d in ds}
        self.assertAlmostEqual(by["s1"]["gain"], 300.0)
        self.assertEqual([m[0] for m in by["s1"]["matched"]], ["30-day"])
        self.assertAlmostEqual(by["s2"]["gain"], 300.0)
        self.assertEqual(by["s2"]["matched"][0][0], "same-day")
        self.assertAlmostEqual(by["s3"]["cost"], 1000.0)
        self.assertAlmostEqual(by["s3"]["gain"], 200.0)

    def test_thirty_day_window_edges(self):
        base = [{"date": D(2024, 1, 1), "side": "BUY", "qty": 10, "value": 1000.0, "id": "a"}]
        # exactly 30 days after the sale: matched (cost 2000, gain -1000)
        inside = base + [
            {"date": D(2024, 2, 1), "side": "BUY", "qty": 10, "value": 2000.0, "id": "b"},
            {"date": D(2024, 1, 2), "side": "SELL", "qty": 10, "value": 1000.0, "id": "s"},
        ]
        ds, _ = analytics.s104(sorted(inside, key=lambda t: t["date"]))
        self.assertAlmostEqual(ds[0]["cost"], 2000.0)
        # 31 days later: not matched, so the pool (cost 1000) is used
        outside = base + [
            {"date": D(2024, 2, 2), "side": "BUY", "qty": 10, "value": 2000.0, "id": "b"},
            {"date": D(2024, 1, 2), "side": "SELL", "qty": 10, "value": 1000.0, "id": "s"},
        ]
        ds, _ = analytics.s104(sorted(outside, key=lambda t: t["date"]))
        self.assertAlmostEqual(ds[0]["cost"], 1000.0)


class TaxSummaryTests(unittest.TestCase):
    def test_tax_year_split_across_5_and_6_april(self):
        c = new_db()
        add_fill(c, "INVEST", 1, "VUAG", "BUY", "2024-01-10", 10, 1000.0)
        add_fill(c, "INVEST", 2, "VUAG", "SELL", "2025-04-05", 5, 600.0, realised=0.0)
        add_fill(c, "INVEST", 3, "VUAG", "SELL", "2025-04-06", 5, 600.0, realised=0.0)
        t = analytics.tax_summary(c, "INVEST", analytics.config())
        years = {y["year"]: y for y in t["years"]}
        self.assertAlmostEqual(years["2024/25"]["gains"], 100.0)
        self.assertAlmostEqual(years["2025/26"]["gains"], 100.0)
        self.assertTrue(years["2024/25"]["flag"])  # T212 says 0, we say 100
        self.assertEqual(years["2025/26"]["taxable"], 0.0)  # below the annual exempt amount


class RunningAvgTests(unittest.TestCase):
    def test_average_cost_after_partial_sale(self):
        trades = [{"ticker": "X", "side": "BUY", "qty": 10, "net": 1000.0, "fees": 0.0},
                  {"ticker": "X", "side": "BUY", "qty": 10, "net": 1200.0, "fees": 0.0},
                  {"ticker": "X", "side": "SELL", "qty": 5, "net": 700.0, "fees": 0.0}]
        avgs, warn = analytics.running_avg(trades)
        self.assertEqual(warn, [])
        qty, avg = avgs["X"]
        self.assertAlmostEqual(qty, 15.0)
        self.assertAlmostEqual(avg, 110.0)


class TwrTests(unittest.TestCase):
    def test_chains_sub_periods_around_a_deposit(self):
        series = [(0, 1000.0), (100, 1100.0), (200, 1600.0)]
        flows = [(200, 500.0)]  # deposit counted in the period ending at t=200
        r, periods = analytics.twr(series, flows)
        self.assertAlmostEqual(r, 0.10)
        self.assertEqual(periods, 2)


class DividendTests(unittest.TestCase):
    def test_ttm_and_yield(self):
        c = new_db()
        add_dividend(c, "ISA", "d1", "AAPL_US_EQ", "2024-11-01", 2.00)  # outside the 12 months
        add_dividend(c, "ISA", "d2", "AAPL_US_EQ", "2025-05-15", 1.07)
        add_position(c, "ISA", "AAPL_US_EQ", 500.0)
        now = dt.datetime(2025, 12, 1, 12).timestamp()
        r = analytics.dividend_report(c, "ISA", now)
        self.assertAlmostEqual(r["ttm"], 1.07)
        self.assertAlmostEqual(r["yield"]["AAPL_US_EQ"], 1.07 / 500.0)
        self.assertEqual(r["by_year"], {2024: 2.0, 2025: 1.07})


class BenchmarkTests(unittest.TestCase):
    def test_deposits_bought_on_their_dates(self):
        c = new_db()
        add_cash(c, "INVEST", "t1", "2021-03-01", "DEPOSIT", 1000.0)   # 10 units @ 100
        add_cash(c, "INVEST", "t2", "2024-08-15", "DEPOSIT", 500.0)    # 4 units @ 125
        add_cash(c, "INVEST", "t3", "2025-09-01", "WITHDRAW", -200.0)  # 1.6 units @ 125
        add_summary(c, "INVEST", dt.datetime(2025, 12, 1, 12), 1800.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "VUAG.csv")
            with open(path, "w", newline="") as f:
                f.write("date,close\n2021-03-01,100\n2024-08-15,125\n2025-12-01,150\n")
            cfg = dict(analytics.config(), benchmark=path)
            b = analytics.benchmark(c, "INVEST", cfg, dt.datetime(2025, 12, 1, 12).timestamp())
        self.assertAlmostEqual(b["bench_value"], 12.4 * 150.0)
        self.assertEqual(b["missing_price_flows"], 0)

    def test_missing_file_is_skipped_with_message(self):
        c = new_db()
        cfg = dict(analytics.config(), benchmark="/nonexistent/VUAG.csv")
        b = analytics.benchmark(c, "INVEST", cfg, 0)
        self.assertIn("no benchmark file", b["skipped"])
        self.assertIn("date,close", b["skipped"])


class RiskTests(unittest.TestCase):
    def test_drawdown_volatility_worst_day(self):
        c = new_db()
        for day, v in ((1, 1000.0), (2, 1200.0), (3, 900.0), (4, 1000.0)):
            add_summary(c, "ISA", dt.datetime(2025, 1, day, 12), v)
        r = analytics.risk(c, "ISA", analytics.config(), dt.datetime(2025, 1, 5, 12).timestamp())
        mdd, rng = r["max_drawdown"]
        self.assertAlmostEqual(mdd, 900 / 1200 - 1)
        self.assertEqual(rng, (D(2025, 1, 2), D(2025, 1, 3)))
        self.assertEqual(r["worst_day"][0], D(2025, 1, 3))
        self.assertAlmostEqual(r["worst_day"][1], 900 / 1200 - 1)
        self.assertEqual(r["from"], D(2025, 1, 1))
        self.assertEqual(r["to"], D(2025, 1, 4))
        self.assertIsNotNone(r["vol"])


class ConcentrationTests(unittest.TestCase):
    def test_only_above_limit_flagged(self):
        rows = [{"ticker": "A", "value": 300.0}, {"ticker": "B", "value": 100.0}]
        out = analytics.concentration_alerts(rows, 25)
        self.assertEqual([t for t, _ in out], ["A"])
        self.assertAlmostEqual(out[0][1], 75.0)

    def test_legacy_rows_without_value_give_no_alerts(self):
        self.assertEqual(analytics.concentration_alerts([{"ticker": "A", "value": None}], 25), [])


class ReportAndSummaryTests(unittest.TestCase):
    def seeded(self):
        c = new_db()
        add_cash(c, "INVEST", "t1", "2025-01-10", "DEPOSIT", 1000.0)
        add_fill(c, "INVEST", 1, "VUAG", "BUY", "2025-01-11", 10, 1000.0)
        add_fill(c, "INVEST", 2, "VUAG", "SELL", "2025-06-10", 4, 480.0, realised=40.0, fees=0.5)
        add_dividend(c, "INVEST", "d1", "VUAG", "2025-06-01", 3.0)
        add_position(c, "INVEST", "VUAG", 720.0, ppl=20.0, cost=700.0)
        add_summary(c, "INVEST", dt.datetime(2025, 6, 30, 12), 750.0)
        add_cash(c, "ISA", "i1", "2025-01-10", "DEPOSIT", 500.0)  # ISA has some history too
        return c

    def test_pnl_realised_counts_sells_only(self):
        p = analytics.pnl(self.seeded(), "INVEST")
        self.assertAlmostEqual(p["realised"], 40.0)
        self.assertAlmostEqual(p["fees_total"], 0.5)

    def test_month_report_writes_csv_with_sale(self):
        c = self.seeded()
        blocks = analytics.month_blocks(c, "INVEST", "2025-06", now=dt.datetime(2025, 7, 1).timestamp())
        out = tempfile.mkdtemp()
        paths = analytics.write_report(blocks, "INVEST", "2025-06", out)
        with open(paths[0], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Trades in month", text)
        self.assertIn("2025-06-10,SELL,VUAG", text)

    def test_summary_text_smoke_and_isa_has_no_tax(self):
        c = self.seeded()
        now = dt.datetime(2025, 7, 1).timestamp()
        inv = analytics.summary_text(c, "INVEST", now=now)
        self.assertIn("Tax (estimate, not tax advice)", inv)
        self.assertIn("Realised (sells)", inv)
        isa = analytics.summary_text(c, "ISA", now=now)
        self.assertIn("not shown for ISA", isa)


if __name__ == "__main__":
    unittest.main()
