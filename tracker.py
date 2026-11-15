#!/usr/bin/env python3
"""Trading 212 tracker: polls your ISA + Invest accounts, stores snapshots,
prints a live table, sends an hourly digest and alerts on big drops."""
import base64
import json
import os
import sqlite3
import sys
import time

import requests

import analytics
import dashboard
import history


def load_env(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env()

BASE = os.getenv("T212_BASE_URL", "https://live.trading212.com/api/v0")
ENDPOINT = os.getenv("T212_POSITIONS_ENDPOINT", "/equity/positions")  # legacy: /equity/portfolio
POLL = int(os.getenv("POLL_SECONDS", "60"))
DROP_PCT = float(os.getenv("DROP_ALERT_PCT", "10"))
MAX_WEIGHT = float(os.getenv("MAX_WEIGHT_PCT", "25"))  # alert when one holding exceeds this share
COOLDOWN = int(os.getenv("ALERT_COOLDOWN_HOURS", "6")) * 3600
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
DB = os.getenv("DB_PATH", "tracker.db")
SYM = os.getenv("CURRENCY_SYMBOL", "£")

ACCOUNTS = {}
for name in ("ISA", "INVEST"):
    key, secret = os.getenv(f"T212_{name}_KEY"), os.getenv(f"T212_{name}_SECRET")
    if key and secret:
        ACCOUNTS[name] = (key, secret)


AVG_KEYS = ("averagePricePaid", "averagePrice")  # API field names to try for average buy price
WARNED = set()


def money(m):
    return f"{'+' if m >= 0 else '-'}{SYM}{abs(m):,.2f}"


def fetch(key, secret):
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    r = requests.get(
        BASE + ENDPOINT, headers={"Authorization": f"Basic {token}"}, timeout=20
    )
    if r.status_code == 429:
        raise RuntimeError("rate limited by Trading 212, will retry")
    r.raise_for_status()
    return r.json()


def fetch_summary(key, secret):
    """Account totals as Trading 212 reports them: cost, current value, profit/loss."""
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    r = requests.get(
        BASE + "/equity/account/summary", headers={"Authorization": f"Basic {token}"}, timeout=20
    )
    r.raise_for_status()
    return r.json()


def save_summary(c, acct, creds, now):
    s = fetch_summary(*creds)
    inv = s.get("investments") or {}
    if "totalCost" not in inv and acct not in WARNED:
        WARNED.add(acct)
        print(f"[{acct}] unexpected summary response, keys: {sorted(s)} / {sorted(inv)}")
    c.execute(
        "INSERT INTO account_summary VALUES (?,?,?,?,?,?,?)",
        (now, acct, s.get("currency"), s.get("totalCash"), inv.get("totalCost"),
         inv.get("currentValue"), inv.get("unrealizedProfitLoss")),
    )
    # raw copy of every poll: total value field names are not confirmed, see README
    c.execute("INSERT INTO summary_snapshots VALUES (?,?,?)", (now, acct, json.dumps(s)))
    c.commit()


def notify(title, body, urgent=False):
    print(f"\n*** {title}\n{body}\n")
    if not NTFY_TOPIC:
        print("push skipped: NTFY_TOPIC is empty (check .env)")
        return
    try:
        r = requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title,  # keep ASCII
                "Priority": "urgent" if urgent else "default",
                "Tags": "rotating_light" if urgent else "chart_with_downwards_trend",
            },
            timeout=10,
        )
        r.raise_for_status()
        print(f"push sent to ntfy (HTTP {r.status_code})")
    except requests.RequestException as e:
        print("notification failed:", e)


def init_db():
    c = sqlite3.connect(DB)  # wait for the dashboard or another run to release the lock
    c.execute(
        "CREATE TABLE IF NOT EXISTS snaps(ts INTEGER, account TEXT, ticker TEXT,"
        " qty REAL, price REAL, ppl REAL)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS i_snaps ON snaps(account, ticker, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS i_snaps_ts ON snaps(ts)")  # dashboard reads new rows by time
    try:  # older databases were created without the average price column
        c.execute("ALTER TABLE snaps ADD COLUMN avg REAL")
    except sqlite3.OperationalError:
        pass
    c.execute("CREATE TABLE IF NOT EXISTS alerts(ts INTEGER, account TEXT, ticker TEXT)")
    c.execute(
        "CREATE TABLE IF NOT EXISTS account_summary(ts INTEGER, account TEXT, currency TEXT,"
        " cash REAL, invested REAL, value REAL, unrealised REAL)"
    )
    history.init_schema(c)  # trade history, positions_latest, summary_snapshots, weight_alerts
    return c


def check_weights(c, acct, parsed, now):
    """Push once per day per holding when it is above MAX_WEIGHT_PCT of the account."""
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    for ticker, w in analytics.concentration_alerts(parsed, MAX_WEIGHT):
        if c.execute("INSERT OR IGNORE INTO weight_alerts VALUES (?,?,?)", (day, acct, ticker)).rowcount:
            sym = ticker.split("_")[0]
            notify(
                f"{sym} is {w:.0f}% of {acct}",
                f"{sym} is {w:.1f}% of your {acct} holdings (limit {MAX_WEIGHT:.0f}%). "
                "Cash is not counted. Consider whether that is what you want.",
            )
    c.commit()


def change(c, acct, ticker, qty, price, ppl, now, window):
    """Change vs the oldest snapshot inside the window.
    Returns (pct, money, age_hours) or None if no usable baseline."""
    row = c.execute(
        "SELECT ts, qty, price, ppl FROM snaps WHERE account=? AND ticker=? AND ts>=?"
        " ORDER BY ts LIMIT 1",
        (acct, ticker, now - window),
    ).fetchone()
    if not row:
        return None
    ts, b_qty, b_price, b_ppl = row
    if abs(b_qty - qty) > 1e-6 or not b_price or now - ts < 60:
        return None  # position size changed (buy/sell) or no real history yet
    return (price / b_price - 1) * 100, ppl - b_ppl, (now - ts) / 3600


def parse_position(p):
    """Normalise one position from either shape the API may return.

    Current /equity/positions: ticker and name under `instrument`, money under
    `walletImpact` (account currency). Legacy /equity/portfolio: flat fields, no
    value or cost. `price` is always in the instrument currency."""
    if "instrument" in p:
        inst = p["instrument"]
        wi = p.get("walletImpact") or {}
        return {
            "ticker": inst["ticker"], "name": inst.get("name") or inst["ticker"],
            "ccy": inst.get("currency"), "qty": p["quantity"], "price": p["currentPrice"],
            "ppl": wi.get("unrealizedProfitLoss", 0.0), "value": wi.get("currentValue"),
            "cost": wi.get("totalCost"), "avg": p.get("averagePricePaid"),
        }
    return {
        "ticker": p["ticker"], "name": p["ticker"], "ccy": None, "qty": p["quantity"],
        "price": p["currentPrice"], "ppl": p.get("ppl", 0.0), "value": None, "cost": None,
        "avg": next((p[k] for k in AVG_KEYS if k in p), None),
    }


def poll_account(c, acct, creds, now, digest):
    positions = fetch(*creds)
    rows = []
    parsed = []
    for raw in positions:
        p = parse_position(raw)
        parsed.append(p)
        t = p["ticker"]
        qty, price, ppl = p["qty"], p["price"], p["ppl"]
        avg = p["avg"]
        if avg is None and acct not in WARNED:
            WARNED.add(acct)
            print(f"[{acct}] no average price in API response, position keys: {sorted(raw)}")
        h1 = change(c, acct, t, qty, price, ppl, now, 3600)
        d1 = change(c, acct, t, qty, price, ppl, now, 86400)
        rows.append((t.split("_")[0], price, h1, d1, ppl))
        c.execute(
            "INSERT INTO snaps(ts, account, ticker, qty, price, ppl, avg) VALUES (?,?,?,?,?,?,?)",
            (now, acct, t, qty, price, ppl, avg),
        )

        if d1 and d1[0] <= -DROP_PCT:
            recent = c.execute(
                "SELECT 1 FROM alerts WHERE account=? AND ticker=? AND ts>?",
                (acct, t, now - COOLDOWN),
            ).fetchone()
            if not recent:
                c.execute("INSERT INTO alerts VALUES (?,?,?)", (now, acct, t))
                last_hour = money(h1[1]) if h1 else "n/a (not enough history yet)"
                notify(
                    f"{t.split('_')[0]} down {abs(d1[0]):.1f}% ({acct})",
                    f"{t.split('_')[0]} in your {acct} is down {abs(d1[0]):.1f}% "
                    f"over the last {d1[2]:.1f}h ({money(d1[1])}).\n"
                    f"Last hour: {last_hour}.\n"
                    f"Have a look at it and decide whether to hold or withdraw.",
                    urgent=True,
                )
    c.commit()

    # latest positions for analytics (replaced on every poll), then concentration check
    c.execute("DELETE FROM positions_latest WHERE account=?", (acct,))
    for p in parsed:
        c.execute(
            "INSERT INTO positions_latest VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (acct, now, p["ticker"], p["name"], p["ccy"], p["qty"], p["price"], p["ppl"],
             p["value"], p["cost"], p["avg"]),
        )
    c.commit()
    check_weights(c, acct, parsed, now)

    # console table
    print(f"\n[{acct}] {time.strftime('%H:%M:%S')}")
    print(f"{'Fund':<10}{'Price':>10}{'1h %':>8}{'1h '+SYM:>11}{'24h %':>8}{'24h '+SYM:>12}")
    for n, price, h1, d1, _ in sorted(rows, key=lambda r: (r[3] or (0, 0, 0))[0]):
        f = lambda x, i, fmt: fmt(x[i]) if x else "-"
        print(
            f"{n:<10}{price:>10,.2f}{f(h1,0,lambda v:f'{v:+.1f}'):>8}"
            f"{f(h1,1,money):>11}{f(d1,0,lambda v:f'{v:+.1f}'):>8}{f(d1,1,money):>12}"
        )

    if digest:
        hs = [r[2] for r in rows if r[2]]
        ds = [r[3] for r in rows if r[3]]
        worst = sorted((r for r in rows if r[2]), key=lambda r: r[2][1])[:3]
        lines = [
            f"{acct} last hour: {money(sum(h[1] for h in hs))}",
            f"{acct} last 24h: {money(sum(d[1] for d in ds))}",
        ]
        if worst:
            lines.append(
                "Weakest 1h: " + ", ".join(f"{r[0]} {money(r[2][1])}" for r in worst)
            )
        notify(f"Hourly update ({acct})", "\n".join(lines))


def main():
    if "--test-push" in sys.argv:  # sends one push to check the phone setup
        notify("Test push", "If you can read this on your phone, ntfy is working.")
        return
    if not ACCOUNTS:
        raise SystemExit("No API keys found. Create a .env file with your keys (see README).")
    c = init_db()
    print(f"Tracking: {', '.join(ACCOUNTS)} | every {POLL}s | alert at -{DROP_PCT}%")
    last_digest = 0
    while True:
        now = int(time.time())
        digest = now - last_digest >= 3600 and last_digest != 0
        for acct, creds in ACCOUNTS.items():
            try:
                poll_account(c, acct, creds, now, digest)
            except Exception as e:  # keep running through network/API hiccups
                print(f"[{acct}] error: {e}")
            try:
                save_summary(c, acct, creds, now)
            except Exception as e:
                print(f"[{acct}] summary error: {e}")
        try:
            dashboard.build(DB)
        except Exception as e:
            print("dashboard error:", e)
        if last_digest == 0 or digest:
            last_digest = now
        time.sleep(POLL)


if __name__ == "__main__":
    main()
