"""Step 1: history sync (paging, 429, 403, idempotent re-sync, alerts) and CSV import.
Offline only: requests.get is replaced with a fake router, requests.post must never run,
and notify is a list collector, so nothing reaches Trading 212 or ntfy."""
import copy
import contextlib
import io
import json
import os
import sqlite3
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import requests  # noqa: E402
import history  # noqa: E402

FIX = os.path.join(ROOT, "fixtures")
CREDS = ("key", "secret")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


class FakeResp:
    def __init__(self, body=None, status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self.body


def router(routes):
    """routes: {path fragment: [FakeResp, ...]}. Empty queue gives an empty last page."""
    calls = []

    def get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        for frag, queue in routes.items():
            if frag in url:
                if queue:
                    return queue.pop(0)
                return FakeResp({"items": [], "nextPagePath": None})
        if "/equity/history/" in url:  # endpoint not stubbed in this test: empty page
            return FakeResp({"items": [], "nextPagePath": None})
        raise AssertionError(f"unexpected URL in test: {url}")
    return get, calls


class Base(unittest.TestCase):
    def setUp(self):
        self.db = history.init_schema(sqlite3.connect(":memory:"))
        self.notes = []
        self.patches = [
            mock.patch.object(history, "MIN_INTERVAL", 0),
            mock.patch.object(history.time, "sleep"),
            mock.patch.object(history.requests, "post", side_effect=AssertionError("no POST in tests")),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def run_sync(self, routes):
        get, calls = router(routes)
        out = io.StringIO()
        with mock.patch.object(history.requests, "get", side_effect=get), \
             contextlib.redirect_stdout(out):
            counts = history.sync_account(self.db, "ISA", CREDS,
                                          lambda t, b, **k: self.notes.append((t, b)))
        return counts, calls, out.getvalue()

    def count(self, table, acct="ISA"):
        return self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE account=?", (acct,)).fetchone()[0]


class PaginationTests(Base):
    def test_full_backfill_follows_next_page_verbatim(self):
        counts, calls, _ = self.run_sync({
            "/equity/history/orders": [FakeResp(fixture("orders_page1.json")),
                                       FakeResp(fixture("orders_page2.json"))],
            "/equity/history/transactions": [FakeResp(fixture("transactions.json"))],
            "/equity/history/dividends": [FakeResp(fixture("dividends.json"))],
        })
        self.assertIsNotNone(counts)
        orders_calls = [u for u in calls if "/history/orders" in u]
        self.assertEqual(len(orders_calls), 2)
        self.assertEqual(orders_calls[0], "https://live.trading212.com/api/v0/equity/history/orders?limit=50")
        self.assertEqual(orders_calls[1],
                         "https://live.trading212.com/api/v0/equity/history/orders?limit=50&cursor=1749566400000")
        self.assertEqual(self.count("orders_hist"), 2)
        self.assertEqual(self.count("fills"), 2)
        self.assertEqual(self.count("dividends"), 1)
        self.assertEqual(self.count("cash_tx"), 4)

    def test_raw_json_kept(self):
        self.run_sync({"/equity/history/orders": [FakeResp(fixture("orders_page1.json"))]})
        raw = self.db.execute("SELECT raw FROM fills WHERE id='5002'").fetchone()[0]
        self.assertEqual(json.loads(raw)["fill"]["walletImpact"]["realisedProfitLoss"], 41.2)

    def test_resync_is_idempotent_and_stops_after_one_page(self):
        self.run_sync({"/equity/history/orders": [FakeResp(fixture("orders_page1.json"))]})
        before = (self.count("orders_hist"), self.count("fills"))
        counts, calls, _ = self.run_sync({
            "/equity/history/orders": [FakeResp(fixture("orders_page1.json"))],
        })
        self.assertEqual((self.count("orders_hist"), self.count("fills")), before)
        self.assertEqual(len([u for u in calls if "/history/orders" in u]), 1)
        self.assertEqual(counts["orders"], 0)

    def test_incremental_pages_until_a_page_has_nothing_new(self):
        self.run_sync({"/equity/history/orders": [FakeResp(fixture("orders_page1.json"))]})
        newer = {"items": [{"order": {"id": 1003, "ticker": "VUAG_EQ", "side": "BUY", "status": "FILLED",
                                      "createdAt": "2026-01-05T10:00:00Z", "currency": "GBP",
                                      "instrument": {"ticker": "VUAG_EQ", "name": "Vanguard"}},
                            "fill": {"id": 5003, "filledAt": "2026-01-05T10:00:02Z", "price": 95.0,
                                     "quantity": 1.0, "type": "TRADE",
                                     "walletImpact": {"currency": "GBP", "netValue": 95.0,
                                                      "realisedProfitLoss": 0.0, "taxes": []}}}],
                 "nextPagePath": "/api/v0/equity/history/orders?limit=50&cursor=1"}
        counts, calls, _ = self.run_sync({"/equity/history/orders": [
            FakeResp(newer),
            FakeResp(fixture("orders_page1.json")),  # all known: stop here
            FakeResp(fixture("orders_page2.json")),  # must not be requested
        ]})
        self.assertEqual(len([u for u in calls if "/history/orders" in u]), 2)
        self.assertEqual(self.count("orders_hist"), 3)
        self.assertEqual(counts["orders"], 2)  # new order and its fill


class RateLimitTests(Base):
    def test_429_sleeps_until_reset_then_retries(self):
        reset = str(int(time.time()) + 7)
        counts, calls, out = self.run_sync({"/equity/history/orders": [
            FakeResp({}, status=429, headers={"x-ratelimit-reset": reset}),
            FakeResp(fixture("orders_page1.json")),
            FakeResp(fixture("orders_page2.json")),
        ]})
        self.assertIsNotNone(counts)
        self.assertEqual(len([u for u in calls if "/history/orders" in u]), 3)  # 429 + 2 pages
        self.assertEqual(self.count("orders_hist"), 2)
        slept = [c.args[0] for c in history.time.sleep.call_args_list if c.args[0] >= 1]
        self.assertTrue(any(5 <= s <= 9 for s in slept), slept)
        self.assertIn("rate limited", out)


class ScopeTests(Base):
    def test_403_names_the_missing_scope_and_skips_account(self):
        counts, _, out = self.run_sync({"/equity/history/orders": [FakeResp({}, status=403)]})
        self.assertIsNone(counts)
        self.assertIn("history:orders", out)
        self.assertIn("Skipping", out)
        self.assertEqual(self.count("orders_hist"), 0)

    def test_403_on_dividends_reports_that_scope(self):
        _, _, out = self.run_sync({
            "/equity/history/orders": [FakeResp(fixture("orders_page2.json"))],
            "/equity/history/dividends": [FakeResp({}, status=403)],
        })
        self.assertIn("history:dividends", out)


class AlertTests(Base):
    def order(self, oid, created, side="BUY"):
        return {"items": [{"order": {"id": oid, "ticker": "VUAG_EQ", "side": side, "status": "FILLED",
                                     "createdAt": created, "currency": "GBP",
                                     "instrument": {"ticker": "VUAG_EQ"}},
                           "fill": {"id": oid + 9000, "filledAt": created, "price": 91.2,
                                    "quantity": 3.2, "type": "TRADE",
                                    "walletImpact": {"currency": "GBP", "netValue": 291.84,
                                                     "realisedProfitLoss": 0.0, "taxes": []}}}],
                "nextPagePath": None}

    def test_first_backfill_sends_nothing(self):
        self.run_sync({"/equity/history/orders": [FakeResp(self.order(1001, "2025-04-02T09:30:00Z"))]})
        self.assertEqual(self.notes, [])

    def test_only_orders_newer_than_last_sync_alert(self):
        self.run_sync({"/equity/history/orders": [FakeResp(self.order(1001, "2025-04-02T09:30:00Z"))]})
        self.run_sync({"/equity/history/orders": [FakeResp(self.order(1002, "2026-02-03T11:00:00Z"))]})
        self.assertEqual(len(self.notes), 1)
        title, body = self.notes[0]
        self.assertEqual(body, "BUY 3.2 VUAG @ 91.20, ISA")
        # repeating the same sync must not alert again
        self.run_sync({"/equity/history/orders": [FakeResp(self.order(1002, "2026-02-03T11:00:00Z"))]})
        self.assertEqual(len(self.notes), 1)


class TransactionsAndDividendsTests(Base):
    def test_transactions_are_normalised_by_type_later(self):
        self.run_sync({"/equity/history/transactions": [FakeResp(fixture("transactions.json"))]})
        rows = self.db.execute("SELECT type, amount FROM cash_tx ORDER BY date_time").fetchall()
        self.assertEqual(rows[2], ("WITHDRAW", -200.0))  # sign kept as received, analytics uses abs()
        self.assertEqual(len(rows), 4)

    def test_transaction_resync_adds_nothing(self):
        self.run_sync({"/equity/history/transactions": [FakeResp(fixture("transactions.json"))]})
        counts, calls, _ = self.run_sync({"/equity/history/transactions": [FakeResp(fixture("transactions.json"))]})
        self.assertEqual(counts["transactions"], 0)
        self.assertEqual(len([u for u in calls if "/transactions" in u]), 1)
        self.assertEqual(self.count("cash_tx"), 4)


class CsvImportTests(Base):
    def test_import_sample_export(self):
        res = history.import_csv(self.db, os.path.join(FIX, "export_sample.csv"), "ISA")
        self.assertEqual(res["fills"], 2)
        self.assertEqual(res["dividends"], 1)
        self.assertEqual(res["cash"], 2)
        self.assertEqual(len(res["unknown"]), 1)
        self.assertIn("Stock split open", res["unknown"][0][1])
        sell = self.db.execute("SELECT side, quantity, realised_pl, fees, source FROM fills"
                               " WHERE side='SELL'").fetchone()
        self.assertEqual(sell, ("SELL", 2.0, 41.2, 0.44, "csv"))
        div = self.db.execute("SELECT amount, div_type FROM dividends").fetchone()
        self.assertEqual(div, (1.07, "ORDINARY"))

    def test_reimport_is_idempotent(self):
        history.import_csv(self.db, os.path.join(FIX, "export_sample.csv"), "ISA")
        before = self.count("fills"), self.count("cash_tx"), self.count("dividends")
        res = history.import_csv(self.db, os.path.join(FIX, "export_sample.csv"), "ISA")
        self.assertEqual((res["fills"], res["cash"], res["dividends"]), (0, 0, 0))
        self.assertEqual((self.count("fills"), self.count("cash_tx"), self.count("dividends")), before)

    def test_headers_matched_by_name_not_position(self):
        path = os.path.join(FIX, "_tmp_reordered.csv")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write("Ticker,Extra column,Action,Total (GBP),Time,No. of shares,Price / share\n"
                    "VUAG,whatever,Market buy,100.00,2025-04-02 09:30:02,1,100\n")
        res = history.import_csv(self.db, path, "INVEST")
        self.assertEqual(res["fills"], 1)
        row = self.db.execute("SELECT ticker, quantity, net_value, account FROM fills").fetchone()
        self.assertEqual(row, ("VUAG", 1.0, 100.0, "INVEST"))


if __name__ == "__main__":
    unittest.main()
