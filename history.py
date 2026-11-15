#!/usr/bin/env python3
"""Trading 212 trade history: sync orders, fills, dividends and cash transactions
into tracker.db, import the app's CSV export as a fallback, and print reports.

  python history.py sync [--account ISA|INVEST|all]
  python history.py import-csv path.csv --account ISA [--currency GBP]
  python history.py report --month YYYY-MM [--account ISA|INVEST|all]
  python history.py summary [--account ISA|INVEST|all]

Sync is safe to repeat: rows are inserted with INSERT OR IGNORE, and after the
first full backfill each endpoint stops paging once a page holds nothing new."""
import base64
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from urllib.parse import urlsplit

import requests

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=os.path.join(HERE, ".env")):
    """Same loader as tracker.py (setdefault, so real environment variables win)."""
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env()

BASE = os.getenv("T212_BASE_URL", "https://live.trading212.com/api/v0")
DB = os.getenv("DB_PATH", "tracker.db")
NTFY_ALERTS = os.getenv("HISTORY_ALERTS", "1") != "0"  # set 0 to silence order alerts
PAGE_LIMIT = 50
MIN_INTERVAL = 3.1       # history endpoints allow 20 requests/min per account
MAX_RETRIES = 5
FILLED = ("FILLED", "PARTIALLY_FILLED")
SCOPES = {
    "orders": "history:orders",
    "dividends": "history:dividends",
    "transactions": "history:transactions",
}
ACCOUNT_NAMES = ("ISA", "INVEST")


class ApiError(Exception):
    pass


class ScopeError(ApiError):
    pass


class AuthError(ApiError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders_hist(account TEXT, id TEXT, ticker TEXT, side TEXT, type TEXT,
  status TEXT, created_at TEXT, quantity REAL, filled_quantity REAL, filled_value REAL,
  currency TEXT, raw TEXT, PRIMARY KEY(account, id));
CREATE TABLE IF NOT EXISTS fills(account TEXT, id TEXT, order_id TEXT, ticker TEXT, name TEXT,
  isin TEXT, side TEXT, fill_type TEXT, trading_method TEXT, filled_at TEXT, price REAL,
  quantity REAL, instrument_ccy TEXT, wallet_ccy TEXT, net_value REAL, realised_pl REAL,
  fees REAL, source TEXT, raw TEXT, PRIMARY KEY(account, id));
CREATE TABLE IF NOT EXISTS dividends(account TEXT, reference TEXT, ticker TEXT, paid_on TEXT,
  quantity REAL, amount REAL, amount_eur REAL, gross_per_share REAL, currency TEXT,
  div_type TEXT, source TEXT, raw TEXT, PRIMARY KEY(account, reference));
CREATE TABLE IF NOT EXISTS cash_tx(account TEXT, reference TEXT, date_time TEXT, type TEXT,
  amount REAL, currency TEXT, source TEXT, raw TEXT, PRIMARY KEY(account, reference));
CREATE TABLE IF NOT EXISTS sync_state(account TEXT, endpoint TEXT, key TEXT, value TEXT,
  PRIMARY KEY(account, endpoint, key));
CREATE TABLE IF NOT EXISTS summary_snapshots(ts INTEGER, account TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS i_summary_snap ON summary_snapshots(account, ts);
CREATE TABLE IF NOT EXISTS positions_latest(account TEXT, ts INTEGER, ticker TEXT, name TEXT,
  ccy TEXT, qty REAL, price REAL, ppl REAL, value REAL, cost REAL, avg REAL,
  PRIMARY KEY(account, ticker));
CREATE TABLE IF NOT EXISTS weight_alerts(day TEXT, account TEXT, ticker TEXT,
  PRIMARY KEY(day, account, ticker));
"""


def init_schema(c):
    c.executescript(SCHEMA)
    return c


def connect(path=None):
    return init_schema(sqlite3.connect(path or DB))


def get_state(c, acct, endpoint, key, default=None):
    row = c.execute("SELECT value FROM sync_state WHERE account=? AND endpoint=? AND key=?",
                    (acct, endpoint, key)).fetchone()
    return row[0] if row else default


def set_state(c, acct, endpoint, key, value):
    c.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?,?,?)",
              (acct, endpoint, key, str(value)))


def _root():
    p = urlsplit(BASE)
    return f"{p.scheme}://{p.netloc}"


def next_url(data):
    """nextPagePath is used verbatim, prefixed with the host only."""
    p = data.get("nextPagePath")
    if not p:
        return None
    return p if p.startswith("http") else _root() + p


class Client:
    """Authenticated GET with the history rate limit (20/min) and 429 handling."""

    def __init__(self, key, secret):
        self.token = base64.b64encode(f"{key}:{secret}".encode()).decode()
        self._last = 0.0

    def get(self, url, scope):
        for _ in range(MAX_RETRIES):
            wait = MIN_INTERVAL - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            r = requests.get(url, headers={"Authorization": f"Basic {self.token}"}, timeout=30)
            if r.status_code == 429:
                reset = r.headers.get("x-ratelimit-reset")
                delay = max(1.0, float(reset) - time.time()) if reset else 60.0
                print(f"  rate limited, waiting {delay:.0f}s")
                time.sleep(delay)
                continue
            if r.status_code == 403:
                raise ScopeError(f"API key is missing the '{scope}' scope (403). "
                                 "Create a new key with it, or update the existing one.")
            if r.status_code == 401:
                raise AuthError("401 unauthorised: check the API key and secret in .env")
            if r.status_code >= 400:
                raise ApiError(f"HTTP {r.status_code} from {url.split('?')[0]}")
            return r.json()
        raise ApiError("still rate limited after several retries, try again later")


# ---------------------------------------------------------------- storing

def _paged(c, acct, client, endpoint, path, scope, store_page):
    """Walk pages newest first. store_page(items) returns how many rows were new.
    First run (no 'backfilled' flag) walks everything. Later runs stop at the first
    page that adds nothing."""
    backfilled = get_state(c, acct, endpoint, "backfilled") == "1"
    url = f"{BASE}{path}?limit={PAGE_LIMIT}"
    total_new = 0
    while url:
        data = client.get(url, scope)
        new = store_page(data.get("items") or [])
        c.commit()
        total_new += new
        url = next_url(data)
        if backfilled and new == 0:
            break
    set_state(c, acct, endpoint, "backfilled", "1")
    c.commit()
    return total_new


def _order_message(acct, o, f):
    sym = (o.get("ticker") or "?").split("_")[0]
    ccy = o.get("currency") or ""
    price = f.get("price")
    qty = f.get("quantity")
    px = f"{price:.2f}" if price is not None else "?"
    q = f"{qty:g}" if qty is not None else "?"
    suffix = "" if ccy in ("", "GBP") else f" {ccy}"
    return f"{o.get('side')} {sym}", f"{o.get('side')} {q} {sym} @ {px}{suffix}, {acct}"


def sync_orders(c, acct, client, notify_fn=None):
    prev_last = get_state(c, acct, "orders", "last_created", "")
    was_backfilled = get_state(c, acct, "orders", "backfilled") == "1"
    fresh = []
    newest = [prev_last]

    def store(items):
        new = 0
        for it in items:
            o = it.get("order") or {}
            f = it.get("fill")
            if (o.get("createdAt") or "") > newest[0]:
                newest[0] = o.get("createdAt") or ""
            cur = c.execute(
                "INSERT OR IGNORE INTO orders_hist VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (acct, str(o.get("id")), o.get("ticker"), o.get("side"), o.get("type"),
                 o.get("status"), o.get("createdAt"), o.get("quantity"),
                 o.get("filledQuantity"), o.get("filledValue"), o.get("currency"),
                 json.dumps(o)))
            new += cur.rowcount
            if f:
                cur = c.execute(
                    "INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    fill_row(acct, o, f))
                new += cur.rowcount
                if cur.rowcount and o.get("status") in FILLED:
                    fresh.append((o, f))
        return new

    n = _paged(c, acct, client, "orders", "/equity/history/orders", SCOPES["orders"], store)
    set_state(c, acct, "orders", "last_created", newest[0])
    c.commit()
    if was_backfilled and notify_fn and NTFY_ALERTS:
        for o, f in fresh:
            if (o.get("createdAt") or "") > prev_last:  # only orders newer than last sync
                title, body = _order_message(acct, o, f)
                notify_fn(title, body)
    return n


def fill_row(acct, o, f):
    wi = f.get("walletImpact") or {}
    taxes = wi.get("taxes") or []
    fees = sum(float(t.get("quantity") or 0) for t in taxes)
    inst = o.get("instrument") or {}
    return (acct, str(f.get("id")), str(o.get("id")), o.get("ticker"), inst.get("name"),
            inst.get("isin"), o.get("side"), f.get("type"), f.get("tradingMethod"),
            f.get("filledAt"), f.get("price"), f.get("quantity"), o.get("currency"),
            wi.get("currency"), wi.get("netValue"), wi.get("realisedProfitLoss"), fees,
            "api", json.dumps({"order": o, "fill": f}))


def sync_dividends(c, acct, client):
    def store(items):
        new = 0
        for d in items:
            cur = c.execute("INSERT OR IGNORE INTO dividends VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (acct, str(d.get("reference")), d.get("ticker"), d.get("paidOn"),
                             d.get("quantity"), d.get("amount"), d.get("amountInEuro"),
                             d.get("grossAmountPerShare"), d.get("currency"), d.get("type"),
                             "api", json.dumps(d)))
            new += cur.rowcount
        return new
    return _paged(c, acct, client, "dividends", "/equity/history/dividends",
                  SCOPES["dividends"], store)


def sync_transactions(c, acct, client):
    def store(items):
        new = 0
        for t in items:
            cur = c.execute("INSERT OR IGNORE INTO cash_tx VALUES (?,?,?,?,?,?,?,?)",
                            (acct, str(t.get("reference")), t.get("dateTime"), t.get("type"),
                             t.get("amount"), t.get("currency"), "api", json.dumps(t)))
            new += cur.rowcount
        return new
    return _paged(c, acct, client, "transactions", "/equity/history/transactions",
                  SCOPES["transactions"], store)


def sync_account(c, acct, creds, notify_fn=None):
    """Returns dict of new-row counts, or None if the account was skipped."""
    client = Client(*creds)
    try:
        counts = {
            "orders": sync_orders(c, acct, client, notify_fn),
            "dividends": sync_dividends(c, acct, client),
            "transactions": sync_transactions(c, acct, client),
        }
    except ScopeError as e:
        print(f"[{acct}] {e} Skipping this account.")
        return None
    except (AuthError, ApiError, requests.RequestException) as e:
        print(f"[{acct}] sync failed: {e}")
        return None
    return counts


# ---------------------------------------------------------------- CSV fallback

def _num(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("£", "").replace("$", "").replace("€", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _find(headers, *names):
    """Header by exact lower-case name, else first header starting with a name ending in '*'."""
    low = {h.strip().lower(): h for h in headers}
    for n in names:
        if n.endswith("*"):
            for lk, orig in low.items():
                if lk.startswith(n[:-1]):
                    return orig
        elif n in low:
            return low[n]
    return None


FEE_PREFIXES = ("stamp duty", "currency conversion fee", "transaction fee", "finra fee",
                "french transaction tax", "ptm levy", "withholding tax", "stamp duty reserve")


def _kind(action):
    a = (action or "").lower()
    if "buy" in a:
        return "BUY"
    if "sell" in a:
        return "SELL"
    if "deposit" in a:
        return "DEPOSIT"
    if "withdrawal" in a:
        return "WITHDRAW"
    if "dividend" in a:
        return "DIVIDEND"
    if "interest" in a:
        return "INTEREST_ON_FREE_CASH"
    if "fee" in a:
        return "FEE"
    return None


def _iso(t):
    """'2025-04-02 09:30:02' -> '2025-04-02T09:30:02Z' (the API uses this form)."""
    t = (t or "").strip().replace(" ", "T")
    if t and not (t.endswith("Z") or re.search(r"[+-]\d\d:?\d\d$", t)):
        t += "Z"
    return t


def import_csv(c, path, acct, currency="GBP"):
    """Import the Trading 212 app's CSV export. Unknown columns are ignored. Returns a
    dict of counts plus 'unknown' rows that need manual review. Re-importing the same
    file adds nothing (deterministic keys)."""
    counts = Counter()
    unknown = []
    seen = Counter()
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        hdr = reader.fieldnames or []
        col = {
            "action": _find(hdr, "action"), "time": _find(hdr, "time"),
            "ticker": _find(hdr, "ticker"), "name": _find(hdr, "name"),
            "isin": _find(hdr, "isin"), "id": _find(hdr, "id"),
            "shares": _find(hdr, "no. of shares", "no. of shares*", "shares*"),
            "price": _find(hdr, "price / share", "price*"),
            "total": _find(hdr, "total", "total (*"),
            "result": _find(hdr, "result", "result (*"),
            "price_ccy": _find(hdr, "currency (price / share)"),
        }
        if not col["action"] or not col["time"]:
            raise SystemExit("CSV needs at least 'Action' and 'Time' columns. Found: " + ", ".join(hdr))
        fee_cols = [h for h in hdr if h.lower().startswith(FEE_PREFIXES)]

        for raw_row in reader:
            row = {str(k): v for k, v in raw_row.items() if k is not None}
            action = row.get(col["action"]) or ""
            kind = _kind(action)
            ref_src = row.get(col["id"]) if col["id"] else None
            if ref_src:
                ref = f"csv:{ref_src.strip()}"
            else:
                base = hashlib.sha1(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]
                seen[base] += 1
                ref = f"csv:{base}:{seen[base]}"
            when = _iso(row.get(col["time"]))
            ticker = (row.get(col["ticker"]) or "").strip() or None
            total = _num(row.get(col["total"])) if col["total"] else None
            taxes = []
            for h in fee_cols:
                v = _num(row.get(h))
                if v:
                    taxes.append({"name": re.sub(r"\W+", "_", h.split("(")[0].strip()).upper(),
                                  "quantity": v, "currency": currency, "chargedAt": when})
            fee_total = sum(t["quantity"] for t in taxes)
            if kind in ("BUY", "SELL"):
                qty = _num(row.get(col["shares"])) if col["shares"] else None
                if not ticker or not qty:
                    unknown.append((when, action, ticker, "missing ticker or share count"))
                    continue
                result = _num(row.get(col["result"])) if col["result"] else None
                wi = {"currency": currency, "netValue": total, "realisedProfitLoss": result or 0.0,
                      "taxes": taxes}
                raw = json.dumps({"walletImpact": wi, "csv": row}, sort_keys=True)
                cur = c.execute("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (acct, ref, None, ticker, row.get(col["name"]) if col["name"] else None,
                                 row.get(col["isin"]) if col["isin"] else None, kind, "TRADE", None,
                                 when, _num(row.get(col["price"])) if col["price"] else None,
                                 abs(qty), row.get(col["price_ccy"]) if col["price_ccy"] else None,
                                 currency, total, result or 0.0, fee_total, "csv", raw))
                counts["fills"] += cur.rowcount
            elif kind == "DIVIDEND":
                m = re.search(r"\((.*?)\)", action)
                dtype = m.group(1).strip().upper().replace(" ", "_") if m else "ORDINARY"
                cur = c.execute("INSERT OR IGNORE INTO dividends VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (acct, ref, ticker, when,
                                 _num(row.get(col["shares"])) if col["shares"] else None,
                                 total, None, None, currency, dtype, "csv", json.dumps(row)))
                counts["dividends"] += cur.rowcount
            elif kind in ("DEPOSIT", "WITHDRAW", "FEE", "INTEREST_ON_FREE_CASH"):
                cur = c.execute("INSERT OR IGNORE INTO cash_tx VALUES (?,?,?,?,?,?,?,?)",
                                (acct, ref, when, kind, total, currency, "csv", json.dumps(row)))
                counts["cash"] += cur.rowcount
            else:
                unknown.append((when, action, ticker, "action not recognised"))
    c.commit()
    return {**counts, "unknown": unknown}


# ---------------------------------------------------------------- CLI

def _accounts(arg):
    if arg in (None, "all"):
        return list(ACCOUNT_NAMES)
    return [arg.upper()]


def _creds(acct):
    key, secret = os.getenv(f"T212_{acct}_KEY"), os.getenv(f"T212_{acct}_SECRET")
    return (key, secret) if key and secret else None


def _argval(args, flag, default=None):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def cmd_sync(args):
    from tracker import notify  # reuses the ntfy sender from tracker.py
    c = connect()
    for acct in _accounts(_argval(args, "--account", "all")):
        creds = _creds(acct)
        if not creds:
            print(f"[{acct}] no API key in .env, skipped")
            continue
        counts = sync_account(c, acct, creds, notify)
        if counts is not None:
            print(f"[{acct}] sync done: " + ", ".join(f"{k} +{v}" for k, v in counts.items()))
    c.close()


def cmd_import(args):
    path = args[0] if args and not args[0].startswith("--") else None
    acct = (_argval(args, "--account") or "").upper()
    if not path or acct not in ACCOUNT_NAMES:
        raise SystemExit("usage: python history.py import-csv path.csv --account ISA|INVEST [--currency GBP]")
    c = connect()
    res = import_csv(c, path, acct, _argval(args, "--currency", "GBP").upper())
    print(f"[{acct}] imported from CSV: " + ", ".join(f"{k} +{v}" for k, v in res.items() if k != "unknown"))
    if res["unknown"]:
        print(f"{len(res['unknown'])} rows need manual review (not imported):")
        for when, action, ticker, why in res["unknown"]:
            print(f"  {when} {action!r} {ticker or ''}: {why}")
    c.close()


def cmd_report(args):
    import analytics
    month = _argval(args, "--month")
    if not month or not re.fullmatch(r"\d{4}-\d{2}", month):
        raise SystemExit("usage: python history.py report --month YYYY-MM [--account ...]")
    c = connect()
    for acct in _accounts(_argval(args, "--account", "all")):
        blocks = analytics.month_blocks(c, acct, month)
        paths = analytics.write_report(blocks, acct, month, os.path.join(HERE, "reports"))
        print(f"[{acct}] wrote " + ", ".join(paths))
    c.close()


def cmd_summary(args):
    import analytics
    c = connect()
    for acct in _accounts(_argval(args, "--account", "all")):
        print(analytics.summary_text(c, acct))
    c.close()


COMMANDS = {"sync": cmd_sync, "import-csv": cmd_import, "report": cmd_report, "summary": cmd_summary}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit(__doc__)
    COMMANDS[sys.argv[1]](sys.argv[2:])
