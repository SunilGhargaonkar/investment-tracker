"""Step 0: /equity/positions (current) and /equity/portfolio (legacy) parsing.
Offline only: requests.get is mocked, nothing is sent to Trading 212 or ntfy."""
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tracker  # noqa: E402

FIX = os.path.join(ROOT, "fixtures")


def load_fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


class ParsePositionTests(unittest.TestCase):
    def test_new_shape_fields(self):
        p = tracker.parse_position(load_fixture("positions_new.json")[0])
        self.assertEqual(p["ticker"], "VUAG_EQ")
        self.assertEqual(p["name"], "Vanguard S&P 500 UCITS ETF")
        self.assertEqual(p["ccy"], "GBP")
        self.assertEqual(p["qty"], 10)
        self.assertEqual(p["price"], 101.5)
        self.assertEqual(p["ppl"], 103.0)      # walletImpact.unrealizedProfitLoss
        self.assertEqual(p["value"], 1015.0)   # walletImpact.currentValue
        self.assertEqual(p["cost"], 912.0)     # walletImpact.totalCost
        self.assertEqual(p["avg"], 91.2)       # averagePricePaid

    def test_legacy_shape_fields(self):
        p = tracker.parse_position(load_fixture("positions_legacy.json")[0])
        self.assertEqual(p["ticker"], "VUAG_EQ")
        self.assertEqual(p["qty"], 10)
        self.assertEqual(p["price"], 101.5)
        self.assertEqual(p["ppl"], 103.0)      # legacy ppl
        self.assertEqual(p["avg"], 91.2)       # legacy averagePrice
        self.assertIsNone(p["value"])          # not provided by the legacy endpoint
        self.assertIsNone(p["cost"])

    def test_both_shapes_give_same_core_numbers(self):
        new = tracker.parse_position(load_fixture("positions_new.json")[0])
        old = tracker.parse_position(load_fixture("positions_legacy.json")[0])
        for k in ("ticker", "qty", "price", "ppl", "avg"):
            self.assertEqual(new[k], old[k], k)


class PollAccountTests(unittest.TestCase):
    """poll_account end to end with the new shape, against a temp DB and mocked HTTP."""

    def test_poll_writes_snapshot_from_new_shape(self):
        import tempfile
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        with mock.patch("tracker.DB", db):
            c = tracker.init_db()
        resp = mock.Mock(status_code=200)
        resp.json.return_value = load_fixture("positions_new.json")
        resp.raise_for_status.return_value = None
        with mock.patch("tracker.requests.get", return_value=resp) as get, \
             mock.patch("tracker.requests.post") as post, \
             mock.patch("tracker.notify"):
            tracker.poll_account(c, "ISA", ("k", "s"), 1_700_000_000, digest=False)
        self.assertTrue(get.call_args[0][0].endswith("/equity/positions"))
        post.assert_not_called()
        row = c.execute("SELECT ticker, qty, price, ppl, avg FROM snaps").fetchone()
        self.assertEqual(row, ("VUAG_EQ", 10, 101.5, 103.0, 91.2))


if __name__ == "__main__":
    unittest.main()
