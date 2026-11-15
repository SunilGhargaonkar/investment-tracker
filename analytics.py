#!/usr/bin/env python3
"""Analytics over the history tables in tracker.db.

Everything here takes a sqlite connection and returns plain numbers or text, so it
can be tested offline. Nothing in this module calls the network. Money is in the
account currency. Estimates are labelled as such in the output."""
import bisect
import csv
import datetime as dt
import json
import math
import os
import sqlite3
import statistics
from collections import Counter, defaultdict, deque

HERE = os.path.dirname(os.path.abspath(__file__))
UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
CONTRIB = ("DEPOSIT", "WITHDRAW")


def config():
    env = os.environ.get
    return {
        "total_key": env("TOTAL_VALUE_KEY", "totalValue"),
        "aea": float(env("CGT_ANNUAL_EXEMPT", "3000")),        # annual exempt amount, GBP
        "rate_basic": float(env("CGT_BASIC_RATE", "0.18")),    # verify against current HMRC rates
        "rate_higher": float(env("CGT_HIGHER_RATE", "0.24")),
        "max_weight": float(env("MAX_WEIGHT_PCT", "25")),
        "benchmark": env("BENCHMARK_CSV", "benchmark/VUAG.csv"),
        "sectors": env("SECTORS_CSV", "sectors.csv"),
        "sym": env("CURRENCY_SYMBOL", "£"),
    }


def _abs_path(p):
    return p if os.path.isabs(p) else os.path.join(HERE, p)


# ---------------------------------------------------------------- small helpers

def to_epoch(s):
    s = (s or "").strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.timestamp()


def to_date(s):
    return dt.date.fromisoformat((s or "")[:10])


def tax_year(d):
    """UK tax year: 6 April to 5 April. 5 April belongs to the year that started the
    previous 6 April. Returned as '2025/26'."""
    y = d.year if (d.month, d.day) >= (4, 6) else d.year - 1
    return f"{y}/{(y + 1) % 100:02d}"


def _loads(s):
    try:
        return json.loads(s) if s else {}
    except ValueError:
        return {}


def dotted(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def summary_total(raw, key):
    """(value, source label). Uses the configured key first. Falls back to
    investments.currentValue + totalCash, the fields the tracker already reads."""
    s = _loads(raw)
    v = dotted(s, key)
    if isinstance(v, (int, float)):
        return float(v), key
    inv = s.get("investments") or {}
    if inv.get("currentValue") is not None and s.get("totalCash") is not None:
        return float(inv["currentValue"]) + float(s["totalCash"]), "investments.currentValue + totalCash"
    return None, None


def _q(c, sql, args=()):
    try:
        return c.execute(sql, args).fetchall()
    except sqlite3.OperationalError:  # table not created yet
        return []


def _dicts(c, sql, args, names):
    return [dict(zip(names, r)) for r in _q(c, sql, args)]


def money(v, sym="£"):
    if v is None:
        return "n/a"
    return f"{'-' if v < 0 else ''}{sym}{abs(v):,.2f}"


def pct(v):
    return "n/a" if v is None else f"{v * 100:+.2f}%"


# ---------------------------------------------------------------- loaders

POS_COLS = ("ticker", "name", "ccy", "qty", "price", "ppl", "value", "cost", "avg", "ts")


def positions(c, acct):
    return _dicts(c, f"SELECT {','.join(POS_COLS)} FROM positions_latest WHERE account=?"
                     " ORDER BY ticker", (acct,), POS_COLS)


FILL_SQL = ("SELECT id, ticker, name, side, fill_type AS type, filled_at, price, quantity AS qty,"
            " instrument_ccy, wallet_ccy, net_value AS net, realised_pl AS realised, fees, raw"
            " FROM fills WHERE account=? ORDER BY filled_at, id")


def fills(c, acct):
    out = []
    for r in _dicts(c, FILL_SQL, (acct,), ("id", "ticker", "name", "side", "type", "filled_at",
                                            "price", "qty", "instrument_ccy", "wallet_ccy",
                                            "net", "realised", "fees", "raw")):
        wi = (_loads(r.pop("raw")).get("walletImpact")) or {}
        r["taxes"] = wi.get("taxes") or []
        r["date"] = to_date(r["filled_at"])
        r["net"] = r["net"] or 0.0
        r["realised"] = r["realised"] or 0.0
        r["fees"] = r["fees"] or 0.0
        r["qty"] = r["qty"] or 0.0
        out.append(r)
    return out


def cash(c, acct):
    out = _dicts(c, "SELECT reference, date_time, type, amount, currency FROM cash_tx WHERE account=?"
                    " ORDER BY date_time, reference", (acct,), ("ref", "when", "type", "amount", "ccy"))
    for r in out:
        r["date"] = to_date(r["when"])
        r["epoch"] = to_epoch(r["when"])
        r["amount"] = r["amount"] or 0.0
    return out


def dividends(c, acct):
    out = _dicts(c, "SELECT reference, ticker, paid_on, amount, div_type AS type FROM dividends"
                    " WHERE account=? ORDER BY paid_on", (acct,),
                 ("ref", "ticker", "when", "amount", "type"))
    for r in out:
        r["date"] = to_date(r["when"])
        r["amount"] = r["amount"] or 0.0
    return out


def summary_series(c, acct, key):
    """[(unix ts, total value)] from every stored account summary poll."""
    out = []
    for ts, raw in _q(c, "SELECT ts, raw FROM summary_snapshots WHERE account=? ORDER BY ts", (acct,)):
        v, _ = summary_total(raw, key)
        if v is not None:
            out.append((ts, v))
    return out


def latest_total(c, acct, key):
    """(value, unix ts, source) for the newest account total we have."""
    for ts, raw in _q(c, "SELECT ts, raw FROM summary_snapshots WHERE account=? ORDER BY ts DESC LIMIT 1",
                      (acct,)):
        v, src = summary_total(raw, key)
        if v is not None:
            return v, ts, src
    for ts, cash_v, value in _q(c, "SELECT ts, cash, value FROM account_summary WHERE account=?"
                                   " ORDER BY ts DESC LIMIT 1", (acct,)):
        if value is not None:
            return value + (cash_v or 0.0), ts, "account_summary (cash + value)"
    return None, None, None


# ---------------------------------------------------------------- maths

def xirr(flows):
    """Money-weighted return. flows: [(date, amount)]; paid in negative, received positive.
    Bisection on the net present value (stdlib only). None if no sign change."""
    flows = sorted(flows)
    if not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    t0 = flows[0][0]

    def npv(r):
        return sum(a / (1 + r) ** ((d - t0).days / 365.0) for d, a in flows)

    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
        if hi - lo < 1e-12:
            break
    return (lo + hi) / 2


def twr(series, flows):
    """Time-weighted return chained over the stored value snapshots.
    series: [(ts, value)]. flows: [(ts, amount)] with deposits positive, withdrawals
    negative; a flow counts in the period that ends at its timestamp. Returns
    (return, number of periods used). Only covers the snapshot range."""
    chain, periods = 1.0, 0
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if v0 <= 0:
            continue
        cf = sum(a for t, a in flows if t0 < t <= t1)
        chain *= (v1 - cf) / v0
        periods += 1
    return (chain - 1.0 if periods else None), periods


def s104(trades):
    """Section 104 pooled average cost with same-day and 30-day (bed and breakfast)
    matching, for one holding.

    trades: chronological dicts with date, side ('BUY'/'SELL'), qty, value, id.
    For BUY, value is the cost including buy fees. For SELL, value is the proceeds
    net of sell fees. Returns (disposals, warnings)."""
    buys = []
    for t in trades:
        if t["side"] == "BUY" and t["qty"] > 0:
            buys.append({"date": t["date"], "unit": t["value"] / t["qty"], "rem": t["qty"], "id": t["id"]})
    pool_q = pool_c = 0.0
    out, warn = [], []
    for d in sorted({t["date"] for t in trades}):
        day_buys = [b for b in buys if b["date"] == d]
        later = [b for b in buys if d < b["date"] <= d + dt.timedelta(days=30)]
        for s in [t for t in trades if t["side"] == "SELL" and t["date"] == d and t["qty"] > 0]:
            left, cost, matched = s["qty"], 0.0, []
            for rule, cands in (("same-day", day_buys), ("30-day", later)):
                for b in cands:
                    if left <= 1e-12:
                        break
                    if b["rem"] <= 1e-12:
                        continue
                    take = min(b["rem"], left)
                    b["rem"] -= take
                    left -= take
                    cost += take * b["unit"]
                    matched.append((rule, take))
            if left > 1e-9:
                take = min(left, pool_q)
                if take > 0:
                    avg = pool_c / pool_q
                    cost += take * avg
                    pool_q -= take
                    pool_c -= take * avg
                    matched.append(("s104", take))
                left -= take
            if left > 1e-9:
                warn.append(f"sale {s['id']} on {d}: {left:g} shares have no matching purchase in the history")
            out.append({"date": d, "id": s["id"], "qty": s["qty"], "proceeds": s["value"],
                        "cost": cost, "gain": s["value"] - cost, "matched": matched})
        for b in day_buys:
            if b["rem"] > 1e-12:
                pool_q += b["rem"]
                pool_c += b["rem"] * b["unit"]
    return out, warn


def running_avg(trades):
    """Average cost per holding through the history (buy fees included in cost).
    trades: chronological dicts with ticker, side, qty, net, fees. Returns
    {ticker: (qty, avg)} after the last trade, and a list of warnings where more was
    sold than the history shows as bought."""
    state = {}
    warn = []
    for t in trades:
        q, cost = state.get(t["ticker"], (0.0, 0.0))
        if t["side"] == "BUY":
            q, cost = q + t["qty"], cost + t["net"] + t["fees"]
        else:
            avg = cost / q if q else 0.0
            if t["qty"] > q + 1e-9:
                warn.append(f"{t['ticker']} sold {t['qty']:g} but history only shows {q:g}")
            take = min(t["qty"], q)
            q, cost = q - take, cost - avg * take
        state[t["ticker"]] = (q, cost)
    return {k: (q, (cost / q if q > 1e-9 else None)) for k, (q, cost) in state.items()}, warn


def fifo_holding_days(trades):
    """Per SELL trade id: average days held, FIFO lots (display only)."""
    lots = defaultdict(deque)
    out = {}
    for t in trades:
        if t["side"] == "BUY":
            lots[t["ticker"]].append([t["date"], t["qty"]])
        elif t["side"] == "SELL":
            left, days = t["qty"], 0.0
            while left > 1e-9 and lots[t["ticker"]]:
                lot = lots[t["ticker"]][0]
                take = min(lot[1], left)
                days += take * (t["date"] - lot[0]).days
                lot[1] -= take
                left -= take
                if lot[1] <= 1e-9:
                    lots[t["ticker"]].popleft()
            done = t["qty"] - left
            out[t["id"]] = days / done if done > 1e-9 else None
    return out


def load_prices(path):
    """Benchmark CSV with columns date,close. Returns sorted [(date, close)]."""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            if r.get("date") and r.get("close"):
                rows.append((dt.date.fromisoformat(r["date"][:10]), float(r["close"].replace(",", ""))))
    return sorted(rows)


def price_on(prices, d):
    """Last close on or before d (None if the file starts later)."""
    i = bisect.bisect_right([p[0] for p in prices], d)
    return prices[i - 1][1] if i else None


def load_sectors(path):
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            if r.get("ticker"):
                out[r["ticker"]] = r.get("sector") or "Unmapped"
    return out


def concentration_alerts(rows, max_pct):
    """rows: dicts with ticker and value (account currency). Returns [(ticker, weight %)]
    for holdings above max_pct of the invested total (cash excluded)."""
    vals = [(r["ticker"], r.get("value")) for r in rows]
    if any(v is None for _, v in vals):
        return []  # legacy endpoint: no market values, so no weights
    total = sum(v for _, v in vals)
    if total <= 0:
        return []
    return [(t, v / total * 100) for t, v in vals if v / total * 100 > max_pct]


# ---------------------------------------------------------------- reports

def pnl(c, acct):
    pos = positions(c, acct)
    fl = fills(c, acct)
    trades = [f for f in fl if f["type"] == "TRADE" and f["side"] in ("BUY", "SELL") and f["qty"] > 0]
    realised = sum(f["realised"] for f in trades if f["side"] == "SELL")
    fees = Counter()
    for f in trades:
        for t in f["taxes"]:
            fees[t.get("name", "OTHER")] += t.get("quantity") or 0.0
    divs = dividends(c, acct)
    avgs, warn = running_avg([{"ticker": f["ticker"], "side": f["side"], "qty": f["qty"],
                               "net": f["net"], "fees": f["fees"]} for f in trades])
    holdings = []
    for p in pos:
        hist_q, hist_avg = avgs.get(p["ticker"], (None, None))
        holdings.append({**p, "hist_avg": hist_avg, "hist_qty": hist_q})
    return {
        "unrealised": sum(p["ppl"] or 0.0 for p in pos) if pos else None,
        "realised": realised,
        "dividends": sum(d["amount"] for d in divs),
        "fees": dict(fees),
        "fees_total": sum(fees.values()),
        "history_from": min((f["date"] for f in fl), default=None),
        "holdings": holdings,
        "warnings": warn,
    }


def contributions(c, acct, cfg, now):
    tx = cash(c, acct)
    dep = sum(abs(x["amount"]) for x in tx if x["type"] == "DEPOSIT")
    wd = sum(abs(x["amount"]) for x in tx if x["type"] == "WITHDRAW")
    other = defaultdict(float)
    for x in tx:
        if x["type"] not in CONTRIB:
            other[x["type"]] += x["amount"]
    value, ts, src = latest_total(c, acct, cfg["total_key"])
    net = dep - wd
    return {"deposits": dep, "withdrawals": wd, "net": net, "other": dict(other),
            "value": value, "value_source": src, "value_ts": ts,
            "growth": (value - net) if value is not None else None}


def returns(c, acct, cfg, now):
    today = dt.date.fromtimestamp(now)
    tx = [x for x in cash(c, acct) if x["type"] in CONTRIB]
    value, _, _ = latest_total(c, acct, cfg["total_key"])
    xflows = [(x["date"], -abs(x["amount"]) if x["type"] == "DEPOSIT" else abs(x["amount"])) for x in tx]
    money_w = None
    if value is not None and xflows:
        money_w = xirr(xflows + [(today, value)])
    series = summary_series(c, acct, cfg["total_key"])
    tflows = [(x["epoch"], abs(x["amount"]) if x["type"] == "DEPOSIT" else -abs(x["amount"])) for x in tx]
    tw, periods = twr(series, tflows) if len(series) > 1 else (None, 0)
    start = dt.date.fromtimestamp(series[0][0]) if series else None
    return {"xirr": money_w, "twr": tw, "twr_periods": periods, "twr_since": start}


def dividend_report(c, acct, now):
    today = dt.date.fromtimestamp(now)
    divs = dividends(c, acct)
    by_month, by_year, by_ticker, by_type = defaultdict(float), defaultdict(float), defaultdict(float), defaultdict(float)
    ttm_ticker = defaultdict(float)
    for d in divs:
        by_month[d["date"].strftime("%Y-%m")] += d["amount"]
        by_year[d["date"].year] += d["amount"]
        by_ticker[d["ticker"]] += d["amount"]
        by_type[d["type"] or "UNKNOWN"] += d["amount"]
        if today - dt.timedelta(days=365) < d["date"] <= today:
            ttm_ticker[d["ticker"]] += d["amount"]
    values = {p["ticker"]: p["value"] for p in positions(c, acct)}
    yields = {}
    for tk, ttm in ttm_ticker.items():
        v = values.get(tk)
        yields[tk] = (ttm / v) if v else None  # yield on current value, account currency
    return {"by_month": dict(sorted(by_month.items())), "by_year": dict(sorted(by_year.items())),
            "by_ticker": dict(by_ticker), "by_type": dict(by_type),
            "ttm": sum(ttm_ticker.values()), "ttm_by_ticker": dict(ttm_ticker), "yield": yields}


def gbp_trades(c, acct):
    """Trade events for the tax calculation, GBP only. BUY value includes fees; SELL
    value is proceeds net of fees. Returns (events by ticker, skipped count)."""
    by_ticker = defaultdict(list)
    skipped = 0
    for f in fills(c, acct):
        if f["type"] != "TRADE" or f["side"] not in ("BUY", "SELL") or f["qty"] <= 0:
            continue
        if (f["wallet_ccy"] or "GBP") != "GBP":
            skipped += 1
            continue
        value = f["net"] + f["fees"] if f["side"] == "BUY" else f["net"] - f["fees"]
        by_ticker[f["ticker"]].append({"date": f["date"], "side": f["side"], "qty": f["qty"],
                                       "value": value, "id": f["id"], "t212": f["realised"]})
    return by_ticker, skipped


def tax_summary(c, acct, cfg):
    """Estimate only, not tax advice. Realised gains by UK tax year from Section 104
    pooled costs with same-day and 30-day matching, in GBP. Losses are not carried
    forward and no other income or allowances are modelled."""
    by_ticker, skipped = gbp_trades(c, acct)
    disposals, warnings = [], []
    for tk, evs in by_ticker.items():
        evs.sort(key=lambda e: e["date"])  # stable: keeps filled_at/id order within a day
        ds, w = s104(evs)
        for d in ds:
            d["ticker"] = tk
            d["t212"] = next((e["t212"] for e in evs if e["id"] == d["id"]), 0.0)
        disposals += ds
        warnings += w
    years = defaultdict(lambda: {"gains": 0.0, "losses": 0.0, "t212": 0.0, "sales": 0})
    for d in disposals:
        y = years[tax_year(d["date"])]
        y["gains" if d["gain"] >= 0 else "losses"] += d["gain"]
        y["t212"] += d["t212"]
        y["sales"] += 1
    rows = []
    for ty in sorted(years):
        y = years[ty]
        net = y["gains"] + y["losses"]
        taxable = max(0.0, net - cfg["aea"])
        rows.append({
            "year": ty, "sales": y["sales"], "gains": y["gains"], "losses": y["losses"], "net": net,
            "aea": cfg["aea"], "taxable": taxable,
            "tax_basic": taxable * cfg["rate_basic"], "tax_higher": taxable * cfg["rate_higher"],
            "t212_realised": y["t212"], "diff_vs_t212": net - y["t212"],
            "flag": abs(net - y["t212"]) > 1.0,
        })
    return {"years": rows, "disposals": disposals, "warnings": warnings, "skipped_non_gbp": skipped}


def trade_activity(c, acct):
    fl = fills(c, acct)
    trades = [f for f in fl if f["type"] == "TRADE" and f["side"] in ("BUY", "SELL") and f["qty"] > 0]
    days = fifo_holding_days([{"ticker": f["ticker"], "side": f["side"], "qty": f["qty"],
                               "date": f["date"], "id": f["id"]} for f in trades])
    timeline = [{"date": f["date"], "side": f["side"], "ticker": f["ticker"], "qty": f["qty"],
                 "price": f["price"], "value": f["net"], "fees": f["fees"],
                 "realised": f["realised"] if f["side"] == "SELL" else None,
                 "avg_days": days.get(f["id"]) if f["side"] == "SELL" else None}
                for f in trades]
    manual = [{"date": f["date"], "type": f["type"], "ticker": f["ticker"], "qty": f["qty"], "id": f["id"]}
              for f in fl if f["type"] != "TRADE"]
    return {"timeline": timeline, "count": len(trades),
            "buys": sum(1 for t in timeline if t["side"] == "BUY"),
            "sells": sum(1 for t in timeline if t["side"] == "SELL"),
            "turnover": sum(abs(t["value"]) for t in timeline),
            "fees": sum(t["fees"] for t in timeline),
            "manual_review": manual}


def benchmark(c, acct, cfg, now):
    path = _abs_path(cfg["benchmark"])
    if not os.path.exists(path):
        return {"skipped": f"no benchmark file at {cfg['benchmark']}. Export daily closes for your "
                           "benchmark (columns date,close) to that path to enable this."}
    prices = load_prices(path)
    if not prices:
        return {"skipped": f"{cfg['benchmark']} has no rows"}
    tx = [x for x in cash(c, acct) if x["type"] in CONTRIB]
    units, flows, missing = 0.0, [], 0
    for x in tx:
        p = price_on(prices, x["date"])
        if p is None:
            missing += 1
            continue
        sign = 1 if x["type"] == "DEPOSIT" else -1
        units += sign * abs(x["amount"]) / p
        flows.append((x["date"], -sign * abs(x["amount"])))
    today = dt.date.fromtimestamp(now)
    bench_value = units * prices[-1][1]
    value, _, _ = latest_total(c, acct, cfg["total_key"])
    out = {"bench_value": bench_value, "bench_last": prices[-1][0], "missing_price_flows": missing,
           "actual_value": value, "xirr_bench": None, "xirr_actual": None}
    if flows:
        out["xirr_bench"] = xirr(flows + [(today, bench_value)])
        if value is not None:
            out["xirr_actual"] = xirr(flows + [(today, value)])
    return out


def risk(c, acct, cfg, now):
    series = summary_series(c, acct, cfg["total_key"])
    tx = [x for x in cash(c, acct) if x["type"] in CONTRIB]
    flow_by_day = defaultdict(float)
    for x in tx:
        flow_by_day[x["date"]] += abs(x["amount"]) if x["type"] == "DEPOSIT" else -abs(x["amount"])
    daily = {}
    for ts, v in series:
        daily[dt.date.fromtimestamp(ts)] = v  # last snapshot of each local day
    days = sorted(daily)
    out = {"days": len(days), "from": days[0] if days else None, "to": days[-1] if days else None,
           "max_drawdown": None, "vol": None, "worst_day": None}
    if len(days) >= 2:
        rets = []
        for a, b in zip(days, days[1:]):
            if daily[a] > 0:
                cf = flow_by_day.get(b, 0.0)
                rets.append((b, (daily[b] - cf) / daily[a] - 1))
        peak, peak_d, mdd, mdd_range = None, None, 0.0, None
        for d in days:
            v = daily[d]
            if peak is None or v > peak:
                peak, peak_d = v, d
            dd = v / peak - 1 if peak else 0.0
            if dd < mdd:
                mdd, mdd_range = dd, (peak_d, d)
        out["max_drawdown"] = (mdd, mdd_range) if mdd_range else (0.0, None)
        if len(rets) >= 2:
            out["vol"] = statistics.stdev([r for _, r in rets]) * math.sqrt(252)
        if rets:
            wd, wr = min(rets, key=lambda x: x[1])
            out["worst_day"] = (wd, wr)
    return out


def allocation(c, acct, cfg):
    pos = [p for p in positions(c, acct) if p["value"] is not None]
    total = sum(p["value"] for p in pos)
    sectors = load_sectors(_abs_path(cfg["sectors"]))
    by_holding = [(p["ticker"], p["name"], p["value"], p["value"] / total * 100 if total else 0) for p in pos]
    by_ccy = defaultdict(float)
    by_sector = defaultdict(float)
    for p in pos:
        by_ccy[p["ccy"] or "?"] += p["value"]
        sym = p["ticker"].split("_")[0]
        by_sector[sectors.get(p["ticker"], sectors.get(sym, "Unmapped"))] += p["value"]
    pct_of = lambda d: [(k, v, v / total * 100 if total else 0) for k, v in sorted(d.items(), key=lambda x: -x[1])]
    return {"holdings": sorted(by_holding, key=lambda x: -x[2]), "currency": pct_of(by_ccy),
            "sector": pct_of(by_sector), "total": total,
            "sectors_file": bool(sectors)}


# ---------------------------------------------------------------- text output

def _table(header, rows):
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
              for i, h in enumerate(header)]
    line = lambda cells: "  " + "  ".join(str(x).ljust(w) if i == 0 else str(x).rjust(w)
                                          for i, (x, w) in enumerate(zip(cells, widths)))
    return "\n".join([line(header)] + [line(r) for r in rows])


def summary_text(c, acct, now=None, cfg=None):
    now = now or dt.datetime.now().timestamp()
    cfg = cfg or config()
    s = cfg["sym"]
    p = pnl(c, acct)
    if not p["holdings"] and not fills(c, acct) and not cash(c, acct):
        return f"[{acct}] no history yet. Run: python history.py sync --account {acct}"
    lines = [f"=== {acct} ===", ""]
    lines.append("P&L (exact, from Trading 212 figures)")
    lines.append(f"  Unrealised (open positions): {money(p['unrealised'], s)}")
    lines.append(f"  Realised (sells):            {money(p['realised'], s)}")
    lines.append(f"  Dividends (all time):        {money(p['dividends'], s)}")
    fee_txt = ", ".join(f"{k.lower()} {money(v, s)}" for k, v in sorted(p["fees"].items())) or "none"
    lines.append(f"  Fees and taxes:              {money(p['fees_total'], s)} ({fee_txt})")
    lines.append(f"  History from:                {p['history_from'] or 'n/a'}")
    if p["holdings"]:
        rows = []
        for h in sorted(p["holdings"], key=lambda h: -(h["value"] or 0)):
            rows.append((h["ticker"].split("_")[0], f"{h['qty']:,.4f}", money(h["value"], s),
                         money(h["ppl"], s), money(h["cost"], s),
                         "n/a" if h["hist_avg"] is None else f"{h['hist_avg']:,.2f}",
                         "n/a" if h["avg"] is None else f"{h['avg']:,.2f}"))
        lines.append(_table(("Holding", "Qty", "Value", "P&L", "Cost (T212)", "Avg (history)",
                             "Avg (T212)"), rows))
    for w in p["warnings"]:
        lines.append(f"  note: {w}")
    lines.append("")

    k = contributions(c, acct, cfg, now)
    lines.append("Contributions and growth")
    lines.append(f"  Deposits {money(k['deposits'], s)}, withdrawals {money(k['withdrawals'], s)}, "
                 f"net contributions {money(k['net'], s)}")
    lines.append(f"  Current total value {money(k['value'], s)} (source: {k['value_source'] or 'none'})")
    lines.append(f"  Growth (value minus net contributions): {money(k['growth'], s)}")
    if k["other"]:
        lines.append("  Not in the contributions sum: " +
                     ", ".join(f"{t.lower()} {money(v, s)}" for t, v in sorted(k["other"].items())))
    lines.append("")

    r = returns(c, acct, cfg, now)
    lines.append("Returns")
    lines.append(f"  Money-weighted (XIRR), from dated deposits and withdrawals: {pct(r['xirr'])}")
    if r["twr"] is not None:
        lines.append(f"  Time-weighted: {pct(r['twr'])}, since tracking started ({r['twr_since']}), "
                     f"{r['twr_periods']} periods. Only covers the snapshot range.")
    else:
        lines.append("  Time-weighted: not enough value snapshots yet")
    lines.append("")

    d = dividend_report(c, acct, now)
    lines.append("Dividends")
    for y, v in d["by_year"].items():
        lines.append(f"  {y}: {money(v, s)}")
    lines.append(f"  Trailing 12 months: {money(d['ttm'], s)}")
    if d["by_type"]:
        lines.append("  By type: " + ", ".join(f"{t} {money(v, s)}" for t, v in d["by_type"].items()))
    yl = sorted(((tk, y) for tk, y in d["yield"].items() if y is not None), key=lambda x: -x[1])
    for tk, y in yl[:10]:
        lines.append(f"  TTM yield {tk.split('_')[0]}: {y * 100:.2f}% on current value")
    lines.append("")

    if acct == "INVEST":
        t = tax_summary(c, acct, cfg)
        lines.append("Tax (estimate, not tax advice). Section 104 in GBP, 6 Apr to 5 Apr tax years")
        lines.append(f"  Annual exempt amount {money(cfg['aea'], s)}, rates "
                     f"{cfg['rate_basic'] * 100:.0f}% / {cfg['rate_higher'] * 100:.0f}% (config, verify)")
        if t["years"]:
            rows = [(y["year"], money(y["gains"], s), money(y["losses"], s), money(y["net"], s),
                     money(y["taxable"], s), money(y["t212_realised"], s),
                     ("CHECK " if y["flag"] else "") + money(y["diff_vs_t212"], s))
                    for y in t["years"]]
            lines.append(_table(("Tax year", "Gains", "Losses", "Net", "Taxable", "T212 realised",
                                 "Difference"), rows))
        else:
            lines.append("  no sells in the history")
        for w in t["warnings"]:
            lines.append(f"  note: {w}")
        if t["skipped_non_gbp"]:
            lines.append(f"  {t['skipped_non_gbp']} fills not in GBP were left out")
        lines.append("")
    else:
        lines.append("Tax: not shown for ISA (gains inside a Stocks ISA are tax-free)")
        lines.append("")

    ta = trade_activity(c, acct)
    lines.append("Trade activity")
    lines.append(f"  {ta['count']} trades ({ta['buys']} buys, {ta['sells']} sells), turnover "
                 f"{money(ta['turnover'], s)}, fees {money(ta['fees'], s)}")
    if ta["manual_review"]:
        lines.append(f"  needs manual review ({len(ta['manual_review'])} non-trade fills, not adjusted):")
        for m in ta["manual_review"][:10]:
            lines.append(f"    {m['date']} {m['type']} {m['ticker']} qty {m['qty']}")
    lines.append("")

    b = benchmark(c, acct, cfg, now)
    lines.append("Benchmark")
    if "skipped" in b:
        lines.append(f"  skipped: {b['skipped']}")
    else:
        lines.append(f"  Same deposits in the benchmark: value {money(b['bench_value'], s)} "
                     f"(latest close {b['bench_last']}), XIRR {pct(b['xirr_bench'])}")
        lines.append(f"  Actual: value {money(b['actual_value'], s)}, XIRR {pct(b['xirr_actual'])}")
    lines.append("")

    rk = risk(c, acct, cfg, now)
    al = allocation(c, acct, cfg)
    lines.append("Risk and concentration")
    if rk["from"]:
        lines.append(f"  Based on {rk['days']} daily snapshots, {rk['from']} to {rk['to']}")
    if rk["max_drawdown"]:
        mdd, rng = rk["max_drawdown"]
        lines.append(f"  Max drawdown: {mdd * 100:.2f}%" + (f" ({rng[0]} to {rng[1]})" if rng else ""))
    if rk["vol"] is not None:
        lines.append(f"  Volatility (annualised, from daily returns): {rk['vol'] * 100:.1f}%")
    if rk["worst_day"]:
        lines.append(f"  Worst day: {rk['worst_day'][0]} {rk['worst_day'][1] * 100:+.2f}%")
    if al["holdings"]:
        lines.append("  Allocation by holding: " + ", ".join(
            f"{t.split('_')[0]} {w:.1f}%" for t, _, _, w in al["holdings"][:8]))
        lines.append("  By currency: " + ", ".join(f"{k} {w:.1f}%" for k, _, w in al["currency"]))
        lines.append("  By sector: " + ", ".join(f"{k} {w:.1f}%" for k, _, w in al["sector"])
                     + ("" if al["sectors_file"] else " (no sectors.csv, all Unmapped)"))
        over = concentration_alerts([{"ticker": t, "value": v} for t, _, v, _ in al["holdings"]],
                                    cfg["max_weight"])
        for t, w in over:
            lines.append(f"  Over {cfg['max_weight']:.0f}% weight: {t.split('_')[0]} {w:.1f}%")
    return "\n".join(lines)


def month_blocks(c, acct, month, now=None, cfg=None):
    """Blocks for the monthly report: (title, header, rows, note)."""
    now = now or dt.datetime.now().timestamp()
    cfg = cfg or config()
    y, m = map(int, month.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m == 12), m % 12 + 1, 1)
    tx = cash(c, acct)
    month_tx = [x for x in tx if start <= x["date"] < end]
    dep = sum(abs(x["amount"]) for x in month_tx if x["type"] == "DEPOSIT")
    wd = sum(abs(x["amount"]) for x in month_tx if x["type"] == "WITHDRAW")
    net_to_end = sum((abs(x["amount"]) if x["type"] == "DEPOSIT" else -abs(x["amount"]))
                     for x in tx if x["type"] in CONTRIB and x["date"] < end)
    end_epoch = dt.datetime(end.year, end.month, end.day, tzinfo=dt.timezone.utc).timestamp()
    value_end = None
    for ts, v in summary_series(c, acct, cfg["total_key"]):
        if ts < end_epoch:
            value_end = v
    fl = [f for f in fills(c, acct) if start <= f["date"] < end and f["type"] == "TRADE"
          and f["side"] in ("BUY", "SELL")]
    realised = sum(f["realised"] for f in fl if f["side"] == "SELL")
    divs = [x for x in dividends(c, acct) if start <= x["date"] < end]
    fees = Counter()
    for f in fl:
        for t in f["taxes"]:
            fees[t.get("name", "OTHER")] += t.get("quantity") or 0.0
    pos = [p for p in positions(c, acct) if p["value"] is not None]
    total = sum(p["value"] for p in pos) or 0.0
    hold = sorted(pos, key=lambda p: p["ppl"] or 0.0)
    rows_h = []
    for p in reversed(hold[-3:]):
        rows_h.append(("top", p["ticker"], p["name"], p["value"], p["ppl"],
                       (p["value"] / total * 100) if total else 0))
    for p in hold[:3]:
        rows_h.append(("bottom", p["ticker"], p["name"], p["value"], p["ppl"],
                       (p["value"] / total * 100) if total else 0))
    return [
        ("Contributions in month", ("item", "amount"),
         [("deposits", round(dep, 2)), ("withdrawals", round(wd, 2)), ("net", round(dep - wd, 2))],
         "From cash transactions dated in the month."),
        ("Position at month end", ("item", "amount"),
         [("net contributions to date", round(net_to_end, 2)),
          ("total value at last snapshot before month end", None if value_end is None else round(value_end, 2)),
          ("growth to date", None if value_end is None else round(value_end - net_to_end, 2))],
         "Total value from stored account summaries. Blank if none was stored before month end."),
        ("Realised P&L in month (Trading 212 figures)", ("item", "amount"),
         [("realised", round(realised, 2))], ""),
        ("Dividends in month", ("date", "ticker", "type", "amount"),
         [(x["date"].isoformat(), x["ticker"], x["type"], round(x["amount"], 2)) for x in divs],
         "Amounts in account currency."),
        ("Fees and taxes in month", ("name", "amount"),
         [(k, round(v, 2)) for k, v in sorted(fees.items())], ""),
        ("Trades in month", ("date", "side", "ticker", "qty", "price", "value", "fees", "realised"),
         [(f["date"].isoformat(), f["side"], f["ticker"], f["qty"], f["price"], round(f["net"], 2),
           round(f["fees"], 2), round(f["realised"], 2) if f["side"] == "SELL" else "")
          for f in fl], "Price is in instrument currency."),
        ("Top and bottom holdings (current, as of report)", ("group", "ticker", "name", "value", "pnl", "weight_pct"),
         [(g, t, n, round(v, 2), round(pl or 0, 2), round(w, 2)) for g, t, n, v, pl, w in rows_h],
         "Current positions, not as of month end."),
    ]


def write_report(blocks, acct, month, outdir):
    """Writes reports/YYYY-MM_<account>.csv and a .md copy. PDF is not produced: reportlab
    is not installed, and the brief asked for md/html in that case."""
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, f"{month}_{acct}")
    with open(base + ".csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        for title, header, rows, note in blocks:
            w.writerow([title])
            w.writerow(header)
            w.writerows(rows)
            if note:
                w.writerow([note])
            w.writerow([])
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(f"# {acct} {month}\n\n")
        for title, header, rows, note in blocks:
            fh.write(f"## {title}\n\n| " + " | ".join(header) + " |\n")
            fh.write("|" + "---|" * len(header) + "\n")
            for r in rows:
                fh.write("| " + " | ".join("" if x is None else str(x) for x in r) + " |\n")
            if note:
                fh.write(f"\n_{note}_\n")
            fh.write("\n")
    return [base + ".csv", base + ".md"]
