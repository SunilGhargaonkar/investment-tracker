#!/usr/bin/env python3
"""Build dashboard.html from tracker.db: profit/loss per holding and for the
whole portfolio (ISA + Invest) over today, 7 days, 30 days, 1 year and all
recorded history, plus each holding hour by hour for a day picked from a
calendar. Open the HTML file in any browser."""
import functools
import html
import json
import math
import os
import sqlite3
import time

DB = os.getenv("DB_PATH", "tracker.db")
OUT = os.getenv("DASHBOARD_PATH", "dashboard.html")
SYM = os.getenv("CURRENCY_SYMBOL", "£")
DAY = 86400
HOURLY_DAYS = int(os.getenv("HOURLY_DAYS", "90"))  # days in the hourly view; older ones stay in the database

# US-listed prices and average buy prices are in USD; profit/loss is in GBP.
# Used to turn current value into GBP. Set GBP_PER_USD in .env to update it.
FX_GBP_PER_USD = float(os.getenv("GBP_PER_USD", "0.74"))

# The API still reports some holdings under their old SPAC/merger codes. Quantities
# match the renamed holding in the Trading 212 app, so these are the same positions.
ALIASES = {
    "YNDX": "NBIS", "VACQ": "RKLB", "IPOB": "OPEN", "IPOE": "SOFI",
    "ARNC": "HWM", "IPAX": "LUNR", "SNII": "RGTI",
}

# Fund names by symbol (after alias). Unknown symbols show the symbol only.
NAMES = {
    "AAPL": "Apple", "AMD": "AMD", "AMZN": "Amazon", "AVGO": "Broadcom",
    "BLK": "BlackRock", "CRSP": "CRISPR Therapeutics", "CRWD": "CrowdStrike",
    "DUOL": "Duolingo", "FB": "Meta Platforms", "GOOGL": "Alphabet",
    "HOOD": "Robinhood", "MSFT": "Microsoft", "MSTR": "Strategy (MicroStrategy)",
    "NET": "Cloudflare", "NFLX": "Netflix", "NVDA": "NVIDIA", "PLTR": "Palantir",
    "SHOP": "Shopify", "UBER": "Uber", "VRT": "Vertiv", "WDC": "Western Digital",
    "AIR": "Airbus", "NBIS": "Nebius Group", "RKLB": "Rocket Lab", "OPEN": "Opendoor Technologies",
    "SOFI": "SoFi Technologies", "HWM": "Howmet Aerospace", "LUNR": "Intuitive Machines",
    "RGTI": "Rigetti Computing", "OPENL": "Opendoor warrants (L)", "OPENW": "Opendoor warrants (W)",
    "OPENZ": "Opendoor warrants (Z)", "VUAGl": "Vanguard S&P 500 UCITS ETF (Acc)",
    "VXUSl": "Vanguard FTSE All-World ex-US",
}

PERIODS = [
    ("Today", "today"),
    ("7 days", 7 * DAY),
    ("30 days", 30 * DAY),
    ("1 year", 365 * DAY),
    ("All recorded", None),
]


def esc(s):
    return html.escape(str(s))


def money(v):
    return f"{'+' if v >= 0 else '-'}{SYM}{abs(v):,.2f}"


def plain(v, fmt="{:,.2f}"):
    return '<span class="na">n/a</span>' if v is None else fmt.format(v)


def direction(v):
    """Css class and icon for a profit/loss value. Icon keeps meaning without colour."""
    if v is None:
        return "na", ""
    if v > 0:
        return "pos", "▲ "
    if v < 0:
        return "neg", "▼ "
    return "", ""


def pl_cell(v, est=False):
    if v is None:
        return '<td class="na">n/a</td>'
    cls, icon = direction(v)
    prefix = "~" if est else ""  # estimate: the position size changed in this period
    return f'<td class="{cls}" title="{"estimated from price move" if est else ""}">{prefix}{icon}{money(v)}</td>'


def period_start(p, now):
    if p == "today":  # local midnight
        t = time.localtime(now)
        return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))
    if p is None:
        return 0
    return now - p


def baseline(c, acct, ticker, start):
    """Last snapshot before the window opens, else the first one inside it."""
    row = c.execute(
        "SELECT ts, qty, ppl, price FROM snaps WHERE account=? AND ticker=? AND ts<?"
        " ORDER BY ts DESC LIMIT 1",
        (acct, ticker, start),
    ).fetchone()
    if not row:
        row = c.execute(
            "SELECT ts, qty, ppl, price FROM snaps WHERE account=? AND ticker=? AND ts>=?"
            " ORDER BY ts LIMIT 1",
            (acct, ticker, start),
        ).fetchone()
    return row


def is_gbp(ticker):
    """GBP-listed lines (e.g. VUAGl_EQ) are already in pounds; US lines are in dollars."""
    return "_US_" not in ticker


def load(c, now):
    # Only the newest poll of each account: positions sold since then drop out.
    latest = c.execute(
        "SELECT s.account, s.ticker, s.qty, s.price, s.ppl, s.avg, s.ts FROM snaps s"
        " JOIN (SELECT account, MAX(ts) AS ts FROM snaps GROUP BY account) m"
        " ON s.account = m.account AND s.ts = m.ts"
    ).fetchall()
    holdings = []
    for acct, ticker, qty, price, ppl, avg, ts in latest:
        sym = ticker.split("_")[0]
        sym = ALIASES.get(sym, sym)
        periods, estimated = {}, set()
        for label, p in PERIODS:
            base = baseline(c, acct, ticker, period_start(p, now))
            if not base or base[0] == ts:
                periods[label] = None  # no earlier data for this window
            elif abs(base[1] - qty) < 1e-6:
                periods[label] = ppl - base[2]  # exact: same position throughout
            elif base[3]:
                # position was bought or sold: estimate from the price move on today's quantity
                periods[label] = (price - base[3]) * qty
                estimated.add(label)
            else:
                periods[label] = None
        gbp_listed = is_gbp(ticker)
        market = qty * price * (1 if gbp_listed else FX_GBP_PER_USD)  # current value, GBP
        holdings.append({
            "acct": acct, "sym": sym, "name": NAMES.get(sym, sym), "qty": qty,
            "ccy": SYM if gbp_listed else "$", "price": price, "ppl": ppl,
            "avg": avg, "periods": periods, "estimated": estimated,
            "invested": market - ppl,  # cost in GBP = current value - profit/loss
        })
    return holdings


def history(c):
    """Total P&L across all snapshots taken at each poll, thinned to ~400 points."""
    rows = c.execute("SELECT ts, SUM(ppl) FROM snaps GROUP BY ts ORDER BY ts").fetchall()
    if len(rows) > 400:
        step = math.ceil(len(rows) / 400)
        thinned = rows[::step]
        if thinned[-1] != rows[-1]:
            thinned.append(rows[-1])
        rows = thinned
    return rows


def load_summary(c):
    """Latest account summary per account, as Trading 212 reports it. Empty if not collected yet."""
    try:
        rows = c.execute(
            "SELECT s.account, s.invested, s.value, s.unrealised FROM account_summary s"
            " JOIN (SELECT account, MAX(ts) AS ts FROM account_summary GROUP BY account) m"
            " ON s.account = m.account AND s.ts = m.ts"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {a: (i, v, u) for a, i, v, u in rows if None not in (i, v, u)}


def account_totals(rows, summary):
    """(invested, current value, unrealised P&L). Uses the Trading 212 summary when present."""
    if summary:
        return summary
    ppl = sum(r["ppl"] for r in rows)
    invested = sum(r["invested"] for r in rows)
    return invested, invested + ppl, ppl


def totals(rows):
    out = {}
    for label, _ in PERIODS:
        vals = [r["periods"][label] for r in rows if r["periods"][label] is not None]
        missing = len(rows) - len(vals)
        out[label] = (sum(vals) if vals else None, missing)
    return out


def nice_ticks(lo, hi, n=4):
    raw = (hi - lo) / n or 1
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    t = math.ceil(lo / step) * step
    ticks = []
    while t <= hi + 1e-9:
        ticks.append(round(t, 10))
        t += step
    return ticks


def compact(v):
    sign = "-" if v < 0 else ""
    a = abs(v)
    return f"{sign}{SYM}{a / 1000:g}K" if a >= 1000 else f"{sign}{SYM}{a:,.0f}"


def line_svg(points):
    if len(points) < 2 or points[-1][0] == points[0][0]:
        return '<p class="note">Not enough history for a chart yet. It fills in as the tracker runs.</p>'
    W, H, L, R, T, B = 900, 280, 64, 24, 16, 30
    x0, x1 = points[0][0], points[-1][0]
    ys = [v for _, v in points]
    lo, hi = min(min(ys), 0), max(max(ys), 0)
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - pad, hi + pad

    def X(t):
        return L + (t - x0) / (x1 - x0) * (W - L - R)

    def Y(v):
        return T + (hi - v) / (hi - lo) * (H - T - B)

    out = []
    for tv in nice_ticks(lo, hi):
        y = Y(tv)
        out.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<text class="axis" x="{L - 8}" y="{y + 4:.1f}" text-anchor="end">{compact(tv)}</text>')
    y0 = Y(0)
    out.append(f'<line class="base" x1="{L}" x2="{W - R}" y1="{y0:.1f}" y2="{y0:.1f}"/>')
    pts = " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in points)
    out.append(f'<polygon class="area" points="{X(x0):.1f},{y0:.1f} {pts} {X(x1):.1f},{y0:.1f}"/>')
    out.append(f'<polyline class="line" points="{pts}"/>')
    out.append(
        f'<circle class="dot" cx="{X(x1):.1f}" cy="{Y(points[-1][1]):.1f}" r="5">'
        f'<title>{money(points[-1][1])}</title></circle>'
    )
    for t, anchor in ((x0, "start"), (x1, "end")):
        when = time.strftime("%d %b %H:%M", time.localtime(t))
        out.append(f'<text class="axis" x="{X(t):.1f}" y="{H - 8}" text-anchor="{anchor}">{when}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Total profit or loss over time">'
            + "".join(out) + "</svg>")


def bars_svg(items):
    """Horizontal bars around a zero line. items: (name, symbol, value)."""
    W, row, top, labelW, plotL, plotR = 900, 26, 8, 190, 270, 820
    vals = [v for _, _, v in items]
    vmin, vmax = min(min(vals), 0), max(max(vals), 0)
    span = (vmax - vmin) or 1

    def X(v):
        return plotL + (v - vmin) / span * (plotR - plotL)

    H = top + row * len(items) + 8
    zero = X(0)
    out = [f'<line class="base" x1="{zero:.1f}" x2="{zero:.1f}" y1="{top}" y2="{H - 8}"/>']
    for i, (name, sym, v) in enumerate(items):
        y, bh = top + i * row, row - 8
        x1 = X(v)
        r = min(4, abs(x1 - zero) / 2, bh / 2)  # 4px rounded data-end, square at the zero line
        if v >= 0:
            d = (f"M{zero:.1f},{y} H{x1 - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x1:.1f},{y + r:.1f}"
                 f" V{y + bh - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x1 - r:.1f},{y + bh} H{zero:.1f} Z")
            tx, anchor, cls, bar = x1 + 6, "start", "val-pos", "bar-pos"
        else:
            d = (f"M{zero:.1f},{y} H{x1 + r:.1f} A{r:.1f},{r:.1f} 0 0 0 {x1:.1f},{y + r:.1f}"
                 f" V{y + bh - r:.1f} A{r:.1f},{r:.1f} 0 0 0 {x1 + r:.1f},{y + bh} H{zero:.1f} Z")
            tx, anchor, cls, bar = x1 - 6, "end", "val-neg", "bar-neg"
        out.append(
            f'<g><title>{esc(name)} [{esc(sym)}]: {money(v)}</title>'
            f'<path class="{bar}" d="{d}"/>'
            f'<text class="nm" x="0" y="{y + bh - 2}">{esc(name)} '
            f'<tspan class="sym">[{esc(sym)}]</tspan></text>'
            f'<text class="{cls}" x="{tx:.1f}" y="{y + bh - 2}" text-anchor="{anchor}">{money(v)}</text></g>'
        )
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Profit or loss per fund">' + "".join(out) + "</svg>"


def tile(label, v, sub="", signed=True):
    cls, icon = direction(v) if signed else ("", "")
    if v is None:
        value = "n/a"
    elif signed:
        value = f"{icon}{money(v)}"
    else:
        value = f"{SYM}{v:,.2f}"
    return (f'<div class="tile"><div class="label">{esc(label)}</div>'
            f'<div class="value {cls}">{value}</div><div class="sub">{sub}</div></div>')


def summary_table(groups, tot):
    """groups: name -> holdings rows. tot: name -> (invested, value, unrealised)."""
    head = "".join(f"<th>{esc(name)}</th>" for name in groups)
    body = []
    body.append('<tr><td class="name">Invested (cost)</td>'
                + "".join(f"<td>{SYM}{tot[n][0]:,.2f}</td>" for n in groups) + "</tr>")
    body.append('<tr><td class="name">Current value</td>'
                + "".join(f"<td>{SYM}{tot[n][1]:,.2f}</td>" for n in groups) + "</tr>")
    body.append('<tr><td class="name">Total P&amp;L (since purchase)</td>'
                + "".join(pl_cell(tot[n][2]) for n in groups) + "</tr>")
    for label, _ in PERIODS:
        cells = []
        for rows in groups.values():
            v, missing = totals(rows)[label]
            cell = pl_cell(v)
            if missing and v is not None:
                cell = cell.replace("</td>", f' <small>({missing} n/a)</small></td>')
            cells.append(cell)
        body.append(f'<tr><td class="name">{label}</td>' + "".join(cells) + "</tr>")
    return (f'<table><thead><tr><th class="name"></th>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table>')


def holdings_table(rows):
    head = (
        '<tr><th class="name">Fund</th><th>Symbol</th><th>Qty</th><th>Avg price</th>'
        "<th>Price</th><th>Invested</th><th>Total P&amp;L</th>"
        + "".join(f"<th>{label}</th>" for label, _ in PERIODS) + "</tr>"
    )
    body = []
    for r in sorted(rows, key=lambda r: r["ppl"], reverse=True):
        body.append(
            f'<tr><td class="name">{esc(r["name"])}</td><td><code>{esc(r["sym"])}</code></td>'
            f'<td>{plain(r["qty"], "{:,.4f}")}</td>'
            f'<td>{"n/a" if r["avg"] is None else r["ccy"] + format(r["avg"], ",.2f")}</td>'
            f'<td>{r["ccy"]}{r["price"]:,.2f}</td><td>{SYM}{r["invested"]:,.2f}</td>'
            + pl_cell(r["ppl"])
            + "".join(pl_cell(r["periods"][label], label in r["estimated"]) for label, _ in PERIODS)
            + "</tr>"
        )
    return f'<table><thead>{head}</thead><tbody>{"".join(body)}</tbody></table>'


def gain_loss_table(groups, holdings):
    """Money gained and lost since purchase, per account and combined."""
    sets = {**groups, "ISA + Invest": holdings}
    head = "".join(f"<th>{esc(name)}</th>" for name in sets)

    def row(label, value):
        cells = "".join(pl_cell(value(rows)) for rows in sets.values())
        return f'<tr><td class="name">{label}</td>{cells}</tr>'

    body = (
        row("Net profit / loss", lambda rows: sum(r["ppl"] for r in rows))
        + row("Money gained (holdings in profit)", lambda rows: sum(r["ppl"] for r in rows if r["ppl"] > 0))
        + row("Money lost (holdings in loss)", lambda rows: sum(r["ppl"] for r in rows if r["ppl"] < 0))
    )
    return (f'<table><thead><tr><th class="name"></th>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table>")


def movers(holdings):
    """Combine each symbol across accounts; top 10 gainers and losers, or all if few."""
    by_sym = {}
    for r in holdings:
        item = by_sym.setdefault(r["sym"], [r["name"], r["sym"], 0.0])
        item[2] += r["ppl"]
    items = sorted((tuple(i) for i in by_sym.values()), key=lambda i: i[2], reverse=True)
    if len(items) > 20:
        items = items[:10] + items[-10:]
    return items


def refresh_hourly(c):
    """Keep snaps_hourly current: the last snapshot of each holding in each local hour.
    Only hours with snapshots since the previous run are rebuilt."""
    c.execute(
        "CREATE TABLE IF NOT EXISTS snaps_hourly(account TEXT, ticker TEXT, hour TEXT, ts INTEGER,"
        " qty REAL, price REAL, ppl REAL, PRIMARY KEY(account, ticker, hour))"
    )
    since = c.execute("SELECT MAX(ts) FROM snaps_hourly").fetchone()[0] or 0
    # SQLite takes the bare columns (qty, price, ppl) from the row holding MAX(ts)
    c.execute(
        "INSERT OR REPLACE INTO snaps_hourly"
        " SELECT account, ticker, strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime') AS h,"
        " MAX(ts), qty, price, ppl FROM snaps WHERE ts >= ? GROUP BY account, ticker, h",
        (since,),
    )
    c.commit()


@functools.lru_cache(maxsize=None)
def hour_start(hour):
    return int(time.mktime(time.strptime(hour, "%Y-%m-%d %H")))


def hourly_changes(c, first_day):
    """Profit/loss per holding per local hour, from first_day ("YYYY-MM-DD") onwards.

    Returns day -> (account, symbol) -> {"v": {hour: money}, "e": [hours], "g": {hour: since}}.
    Each hour is the change from the last snapshot before it to the last one inside it.
    "e": the position was bought or sold, so the figure is estimated from the price move.
    "g": the tracker was not running in the previous hour; the figure covers the gap too."""
    # start a day early so the first hour of first_day has a baseline
    before = time.strftime("%Y-%m-%d", time.localtime(hour_start(first_day + " 00") - DAY))
    rows = c.execute(
        "SELECT account, ticker, hour, ts, qty, price, ppl FROM snaps_hourly"
        " WHERE hour >= ? ORDER BY account, ticker, hour",
        (before,),
    ).fetchall()
    days, prev = {}, None
    for row in rows:
        acct, ticker, hour, ts, qty, price, ppl = row
        if not prev or prev[:2] != (acct, ticker):
            prev = row
            continue
        p_ts, p_qty, p_price, p_ppl = prev[3:]
        prev = row
        day, hh = hour.split(" ")
        if day < first_day:
            continue
        est = abs(qty - p_qty) > 1e-6
        if not est:
            v = ppl - p_ppl
        elif p_price:
            v = (price - p_price) * qty * (1 if is_gbp(ticker) else FX_GBP_PER_USD)
        else:
            continue
        sym = ticker.split("_")[0]
        sym = ALIASES.get(sym, sym)
        cell = days.setdefault(day, {}).setdefault((acct, sym), {"v": {}, "e": [], "g": {}})
        h = int(hh)
        cell["v"][h] = cell["v"].get(h, 0.0) + v
        if est:
            cell["e"].append(h)
        if p_ts < hour_start(hour) - 3600:
            cell["g"][h] = time.strftime("%d %b %H:%M", time.localtime(p_ts))
    return days


def hourly_data(c, now):
    """JSON-ready data for the hourly section: the last HOURLY_DAYS days."""
    refresh_hourly(c)
    first_day = time.strftime("%Y-%m-%d", time.localtime(now - (HOURLY_DAYS - 1) * DAY))
    days = {
        day: [
            {"a": acct, "s": sym, "n": NAMES.get(sym, sym),
             "v": {h: round(v, 2) for h, v in cell["v"].items()}, "e": cell["e"], "g": cell["g"]}
            for (acct, sym), cell in cells.items()
        ]
        for day, cells in hourly_changes(c, first_day).items()
    }
    return {
        "days": days, "sym": SYM, "keep": HOURLY_DAYS,
        "accounts": sorted({r["a"] for rows in days.values() for r in rows}),
        "today": time.strftime("%Y-%m-%d", time.localtime(now)),
    }


CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --base: #c3c2b7; --line: rgba(11,11,11,0.10);
  --series: #2a78d6; --good: #0ca30c; --bad: #d03b3b; --good-text: #006300; --bad-text: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --base: #383835; --line: rgba(255,255,255,0.10);
    --series: #3987e5; --good: #0ca30c; --bad: #d03b3b; --good-text: #0ca30c; --bad-text: #e66767;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px; background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 14px;
}
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0; font-weight: 600; }
.meta { color: var(--ink2); font-size: 13px; margin-top: 4px; }
.card, .hero, .tile {
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
}
.hero { padding: 24px 28px; margin-top: 20px; }
.label { color: var(--ink2); font-size: 13px; }
.hero .value { font-size: 52px; font-weight: 600; line-height: 1.1; margin: 6px 0; }
.sub { color: var(--muted); font-size: 13px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin-top: 12px; }
.tile { padding: 14px 16px; }
.tile .value { font-size: 22px; font-weight: 600; margin: 4px 0; font-variant-numeric: tabular-nums; }
.card { padding: 18px 20px; margin-top: 16px; overflow-x: auto; }
.card h2 { font-size: 15px; font-weight: 600; margin: 0 0 12px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th { font-weight: 500; color: var(--muted); font-size: 12px; text-align: right; padding: 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
td { text-align: right; padding: 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th.name, td.name { text-align: left; }
td.name { font-weight: 500; }
tbody tr:hover { background: var(--page); }
tbody tr:last-child td { border-bottom: none; }
.pos { color: var(--good-text); }
.neg { color: var(--bad-text); }
.na { color: var(--muted); }
small { color: var(--muted); }
code { font-size: 12px; color: var(--ink2); background: var(--page); padding: 1px 6px; border-radius: 4px; }
.note { color: var(--muted); font-size: 12px; margin: 10px 0 0; }
svg { width: 100%; height: auto; display: block; }
svg text { font-size: 12px; fill: var(--ink2); }
svg .axis { fill: var(--muted); }
svg .nm { fill: var(--ink); font-size: 12px; }
svg .sym { fill: var(--muted); }
svg .grid { stroke: var(--grid); stroke-width: 1; }
svg .base { stroke: var(--base); stroke-width: 1; }
svg .line { fill: none; stroke: var(--series); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
svg .area { fill: var(--series); opacity: 0.1; }
svg .dot { fill: var(--series); stroke: var(--surface); stroke-width: 2; }
svg .bar-pos { fill: var(--good); }
svg .bar-neg { fill: var(--bad); }
svg .val-pos { fill: var(--good-text); font-variant-numeric: tabular-nums; }
svg .val-neg { fill: var(--bad-text); font-variant-numeric: tabular-nums; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 4px; }
.controls input, .controls select, .controls button {
  font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--line);
  border-radius: 8px; padding: 6px 10px;
}
.controls button { cursor: pointer; }
.controls button:disabled { color: var(--muted); cursor: default; }
.controls label { color: var(--ink2); font-size: 13px; margin-left: 8px; }
.day-title { font-size: 16px; font-weight: 600; margin: 14px 0 0; }
.empty { color: var(--muted); padding: 16px 0; }
h3 { font-size: 13px; font-weight: 600; color: var(--ink2); margin: 20px 0 8px; }
#h-table th.name, #h-table td.name { position: sticky; left: 0; background: var(--surface); }
#h-table tr.group td { text-align: left; color: var(--ink2); font-size: 12px; font-weight: 600; padding-top: 16px; }
#h-table tfoot td { border-top: 1px solid var(--base); font-weight: 600; }
#h-table .total { border-left: 1px solid var(--grid); font-weight: 600; }
#h-table td.cell { font-size: 13px; }
.mark { color: var(--muted); }
"""

HOURLY_JS = r"""
(() => {
const $ = id => document.getElementById(id);
const days = Object.keys(HOURLY.days).sort();
const fmt = new Intl.NumberFormat("en-GB", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const money = v => (v >= 0 ? "+" : "-") + HOURLY.sym + fmt.format(Math.abs(v));
const cls = v => v > 0 ? "pos" : v < 0 ? "neg" : "";
const icon = v => v > 0 ? "▲ " : v < 0 ? "▼ " : "";
const hh = h => String(h).padStart(2, "0") + ":00";
const span = h => `${hh(h)}–${hh((h + 1) % 24)}`;
const sum = a => a.reduce((s, x) => s + x, 0);

function el(tag, className, text) {  // names go in as text, never as HTML
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}

// chosen day and account live in the URL, so the once-a-minute reload keeps them
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  return {date: p.get("date") || days[days.length - 1] || HOURLY.today, acct: p.get("acct") || "all"};
}
let state = readHash();

function setState(patch) {
  state = {...state, ...patch};
  history.replaceState(null, "", "#" + new URLSearchParams(state));
  render();
}

function tile(label, value, sub) {
  const t = el("div", "tile");
  t.append(el("div", "label", label),
           el("div", "value " + (value === null ? "" : cls(value)), value === null ? "n/a" : icon(value) + money(value)),
           el("div", "sub", sub));
  return t;
}

function niceTicks(lo, hi, n = 4) {
  const raw = (hi - lo) / n || 1, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step) out.push(+t.toFixed(10));
  return out;
}

function compact(v) {
  const a = Math.abs(v), s = v < 0 ? "-" : "";
  return a >= 1000 ? `${s}${HOURLY.sym}${+(a / 1000).toFixed(2)}K` : `${s}${HOURLY.sym}${Math.round(a)}`;
}

function chart(hours, perHour, running) {
  const NS = "http://www.w3.org/2000/svg";
  const W = 900, H = 220, L = 64, R = 16, T = 16, B = 30;
  const svg = document.createElementNS(NS, "svg");
  const add = (parent, tag, attrs, text) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    parent.append(e);
    return e;
  };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Net profit or loss per hour");
  const vals = hours.map(h => perHour[h]);
  let lo = Math.min(...vals, 0), hi = Math.max(...vals, 0);
  const pad = (hi - lo) * 0.08 || 1;
  lo -= pad; hi += pad;
  const Y = v => T + (hi - v) / (hi - lo) * (H - T - B);
  const band = (W - L - R) / hours.length, bw = Math.min(band - 2, 40), y0 = Y(0);
  for (const tv of niceTicks(lo, hi)) {
    add(svg, "line", {class: "grid", x1: L, x2: W - R, y1: Y(tv), y2: Y(tv)});
    add(svg, "text", {class: "axis", x: L - 8, y: Y(tv) + 4, "text-anchor": "end"}, compact(tv));
  }
  hours.forEach((h, i) => {
    const v = perHour[h], cx = L + band * (i + 0.5), x = cx - bw / 2, y1 = Y(v);
    const r = Math.min(4, Math.abs(y1 - y0) / 2, bw / 2);  // 4px rounded data-end, square at zero
    const d = v >= 0
      ? `M${x},${y0} V${y1 + r} A${r},${r} 0 0 1 ${x + r},${y1} H${x + bw - r} A${r},${r} 0 0 1 ${x + bw},${y1 + r} V${y0} Z`
      : `M${x},${y0} V${y1 - r} A${r},${r} 0 0 0 ${x + r},${y1} H${x + bw - r} A${r},${r} 0 0 0 ${x + bw},${y1 - r} V${y0} Z`;
    const g = add(svg, "g", {});
    add(g, "title", {}, `${span(h)}: ${money(v)} (day so far ${money(running[h])})`);
    add(g, "rect", {x: L + band * i, y: T, width: band, height: H - T - B, fill: "transparent"});  // hover target
    add(g, "path", {class: v >= 0 ? "bar-pos" : "bar-neg", d});
    if (band >= 30 || i % 2 === 0) add(svg, "text", {class: "axis", x: cx, y: H - 10, "text-anchor": "middle"}, hh(h));
  });
  add(svg, "line", {class: "base", x1: L, x2: W - R, y1: y0, y2: y0});
  return svg;
}

function cellTitle(r, h, v) {
  let t = `${r.n} [${r.s}] ${r.a}, ${span(h)}: ${money(v)}`;
  if (r.e.includes(h)) t += "\nEstimated: the position was bought or sold in this hour.";
  if (r.g[h]) t += `\nIncludes the change since ${r.g[h]} (tracker was not running in between).`;
  return t;
}

function table(rows, hours, perHour, running, net) {
  const maxAbs = Math.max(...rows.flatMap(r => Object.values(r.v).map(Math.abs)), 0.01);
  const thead = el("thead"), hr = el("tr");
  hr.append(el("th", "name", "Stock"), el("th", "", "Account"));
  hours.forEach(h => hr.append(el("th", "", hh(h))));
  hr.append(el("th", "total", "Day total"));
  thead.append(hr);

  const tbody = el("tbody");
  const group = (label, list) => {
    if (!list.length) return;
    const tr = el("tr", "group");
    const td = el("td", "", `${label} (${list.length}) · ${money(sum(list.map(r => r.total)))}`);
    td.colSpan = hours.length + 3;
    tr.append(td);
    tbody.append(tr);
    for (const r of list) {
      const tr = el("tr"), name = el("td", "name", r.n + " ");
      name.append(el("code", "", r.s));
      tr.append(name, el("td", "", r.a));
      for (const h of hours) {
        const v = r.v[h];
        if (v === undefined) { tr.append(el("td", "na", "–")); continue; }
        const td = el("td", "cell " + cls(v), (r.e.includes(h) ? "~" : "") + money(v));
        if (r.g[h]) td.append(el("span", "mark", "*"));
        td.title = cellTitle(r, h, v);
        if (v) {  // tint grows with the size of the move, relative to the biggest cell that day
          const pct = Math.round(4 + 16 * Math.abs(v) / maxAbs);
          td.style.background = `color-mix(in srgb, var(${v > 0 ? "--good" : "--bad"}) ${pct}%, transparent)`;
        }
        tr.append(td);
      }
      tr.append(el("td", "total " + cls(r.total), icon(r.total) + money(r.total)));
      tbody.append(tr);
    }
  };
  group("Gaining", rows.filter(r => r.total > 0));
  group("Losing", rows.filter(r => r.total < 0));
  group("No change", rows.filter(r => r.total === 0));

  const tfoot = el("tfoot");
  const foot = (label, vals) => {
    const tr = el("tr"), td = el("td", "name", label);
    td.colSpan = 2;
    tr.append(td);
    hours.forEach(h => tr.append(el("td", "cell " + cls(vals[h]), money(vals[h]))));
    tr.append(el("td", "total " + cls(net), icon(net) + money(net)));
    tfoot.append(tr);
  };
  foot("All stocks, per hour", perHour);
  foot("Running total for the day", running);
  const t = el("table");
  t.append(thead, tbody, tfoot);
  return t;
}

function render() {
  const any = days.length > 0;
  $("h-date").value = state.date;
  if (any) { $("h-date").min = days[0]; $("h-date").max = days[days.length - 1]; }
  $("h-prev").disabled = !any || state.date <= days[0];
  $("h-next").disabled = !any || state.date >= days[days.length - 1];
  $("h-acct").value = state.acct;
  $("h-day").textContent = new Date(state.date + "T12:00").toLocaleDateString("en-GB",
    {weekday: "long", day: "numeric", month: "long", year: "numeric"});

  const rows = (HOURLY.days[state.date] || [])
    .filter(r => state.acct === "all" || r.a === state.acct)
    .map(r => ({...r, total: Math.round(sum(Object.values(r.v)) * 100) / 100}))  // drop float noise
    .sort((a, b) => b.total - a.total);
  $("h-body").hidden = !rows.length;
  $("h-empty").hidden = !!rows.length;
  if (!rows.length) {
    $("h-empty").textContent = any
      ? `No snapshots on this day: the tracker was not running, or the day is more than ${HOURLY.keep} days ago. Pick another date.`
      : "No hourly data yet. It fills in as the tracker runs.";
    return;
  }

  const recorded = [...new Set(rows.flatMap(r => Object.keys(r.v).map(Number)))].sort((a, b) => a - b);
  const hours = recorded.filter(h => rows.some(r => r.v[h]));  // hide hours where nothing moved
  const perHour = {}, running = {};
  let acc = 0;
  for (const h of hours) {
    perHour[h] = sum(rows.map(r => r.v[h] || 0));
    running[h] = acc += perHour[h];
  }
  const up = rows.filter(r => r.total > 0), down = rows.filter(r => r.total < 0).reverse();
  const net = sum(rows.map(r => r.total));
  const plural = n => `${n} stock${n === 1 ? "" : "s"}`;
  $("h-tiles").replaceChildren(
    tile("Net for the day", net,
         state.date === HOURLY.today ? `so far, up to ${hh((recorded[recorded.length - 1] + 1) % 24)}` : "whole day"),
    tile("Gained", sum(up.map(r => r.total)), `${plural(up.length)} up`),
    tile("Lost", sum(down.map(r => r.total)), `${plural(down.length)} down`),
    tile("Best stock", up.length ? up[0].total : null, up.length ? `${up[0].n} (${up[0].a})` : "none up"),
    tile("Worst stock", down.length ? down[0].total : null, down.length ? `${down[0].n} (${down[0].a})` : "none down"),
  );
  if (!hours.length) {
    $("h-chart").replaceChildren(el("p", "empty", "No price moves on this day."));
    $("h-table").replaceChildren();
    return;
  }
  $("h-chart").replaceChildren(chart(hours, perHour, running));
  $("h-table").replaceChildren(table(rows, hours, perHour, running, net));
}

$("h-date").addEventListener("change", e => e.target.value && setState({date: e.target.value}));
$("h-acct").addEventListener("change", e => setState({acct: e.target.value}));
const step = dir => {  // jump to the previous/next day that has data
  const next = dir > 0 ? days.find(d => d > state.date) : days.filter(d => d < state.date).pop();
  if (next) setState({date: next});
};
$("h-prev").addEventListener("click", () => step(-1));
$("h-next").addEventListener("click", () => step(1));
$("h-latest").addEventListener("click", () => setState({date: days[days.length - 1] || HOURLY.today}));
window.addEventListener("hashchange", () => { state = readHash(); render(); });
for (const a of HOURLY.accounts) $("h-acct").append(new Option(a, a));
render();
})();
"""


def hourly_section(data):
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return f"""<div class="card" id="hourly"><h2>Hourly profit / loss by stock</h2>
<div class="controls">
  <button id="h-prev" type="button" aria-label="Previous day">← Prev</button>
  <input id="h-date" type="date" aria-label="Day">
  <button id="h-next" type="button" aria-label="Next day">Next →</button>
  <button id="h-latest" type="button">Latest</button>
  <label for="h-acct">Account</label>
  <select id="h-acct"><option value="all">ISA + Invest</option></select>
</div>
<div class="day-title" id="h-day"></div>
<p class="empty" id="h-empty" hidden></p>
<div id="h-body">
<div class="tiles" id="h-tiles"></div>
<h3>Net profit / loss per hour</h3><div id="h-chart"></div>
<h3>Each stock, hour by hour</h3><div id="h-table"></div>
</div>
<p class="note">Hours are local time; hours where no stock moved are hidden. Each hour is the change in profit/loss from the end of the previous
hour to the end of this one. Gainers first, then losers, sorted by day total. "~" means the position
was bought or sold in that hour, so the figure is estimated from the price move. "*" means the tracker
was not running in the hour before, so the figure includes the change over that gap. Profit from
selling a holding (realised) is not included. Hover a bar or cell for details.</p>
<script>const HOURLY = {payload};{HOURLY_JS}</script></div>"""


def build(db=DB, out=OUT):
    if not os.path.exists(db):
        print(f"{db} not found, dashboard skipped. Run tracker.py first.")
        return
    c = sqlite3.connect(db)
    now = int(time.time())
    holdings = load(c, now)
    if not holdings:
        print(f"No snapshots in {db} yet, dashboard skipped.")
        return
    first = c.execute("SELECT MIN(ts) FROM snaps").fetchone()[0]
    points = history(c)

    groups = {}
    for r in holdings:
        groups.setdefault(r["acct"], []).append(r)
    summ = load_summary(c)
    tot = {name: account_totals(rows, summ.get(name)) for name, rows in groups.items()}
    tot["ISA + Invest"] = tuple(sum(t[i] for t in tot.values()) for i in range(3))
    summary = summary_table({**groups, "ISA + Invest": holdings}, tot)

    combined = totals(holdings)
    invested, value, total_ppl = tot["ISA + Invest"]
    hero_sub = (f"{total_ppl / invested * 100:+.1f}% on {SYM}{invested:,.2f} invested"
                if invested else "Invested amount not available yet")
    hero_cls, hero_icon = direction(total_ppl)

    tiles = "".join([
        tile("Current value", value, "ISA + Invest", signed=False),
        tile("Invested", invested, "cost of holdings", signed=False),
        tile("Today", combined["Today"][0], "since local midnight"),
        tile("Last 7 days", combined["7 days"][0]),
        tile("Last 30 days", combined["30 days"][0]),
        tile("Last year", combined["1 year"][0]),
    ])

    accounts = "".join(
        f'<div class="card"><h2>{esc(name)} holdings</h2>{holdings_table(rows)}'
        f'<p class="note">Sorted by total profit or loss. "~" means the position was bought or sold '
        f"in that period, so the figure is estimated from the price move. \"n/a\" means there is no "
        f"earlier data yet.</p></div>"
        for name, rows in groups.items()
    )
    try:
        hourly = hourly_section(hourly_data(c, now))
    except sqlite3.Error as e:  # e.g. database busy; the rest of the dashboard still renders
        hourly = f'<div class="card"><h2>Hourly profit / loss by stock</h2><p class="note">Not available: {esc(e)}</p></div>'
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>setTimeout(() => location.reload(), 60000);  // reload keeps the #date=... chosen below</script>
<title>Trading 212 tracker</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>Trading 212 tracker</h1>
<div class="meta">Updated {time.strftime('%d %b %Y %H:%M', time.localtime(now))}
 · history since {time.strftime('%d %b %Y %H:%M', time.localtime(first))}
 · refreshes every minute · P&amp;L from Trading 212, shown in {esc(SYM)}</div>

<section class="hero">
  <div class="label">Total profit / loss since purchase, ISA + Invest</div>
  <div class="value {hero_cls}">{hero_icon}{money(total_ppl)}</div>
  <div class="sub">{hero_sub}</div>
</section>
<div class="tiles">{tiles}</div>

<div class="card"><h2>Profit / loss by period</h2>{summary}
<p class="note">Periods longer than your recorded history show "n/a" or a partial figure.</p></div>

{hourly}

<div class="card"><h2>Total profit / loss over time, ISA + Invest</h2>{line_svg(points)}</div>

<div class="card"><h2>Biggest movers since purchase, ISA + Invest combined</h2>
<div style="margin-bottom:16px">{gain_loss_table(groups, holdings)}</div>{bars_svg(movers(holdings))}</div>

{accounts}
</div></body></html>
"""
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {out} ({len(holdings)} holdings)")


if __name__ == "__main__":
    build()
