# Changelog

## Unreleased: hourly profit/loss by stock
- `dashboard.html` gains an hourly section: day picker, account filter, tiles (net, gained, lost,
  best, worst), net P&L per hour chart, and a stock-by-hour table with per-hour and running totals.
- New table `snaps_hourly`, refreshed incrementally by `dashboard.build()`. New index `snaps(ts)`.
- Dashboard reload is now a script (keeps the chosen date in the URL) instead of a meta refresh.
- Fix: the latest-snapshot queries in `dashboard.py` were quadratic in the number of snapshots
  (minutes per build at ~150k rows, delaying the tracker's polls). Now a join on `MAX(ts)`.
- Tests: `tests/test_hourly.py`.

## Unreleased: trade history and analytics

### Step 0: positions endpoint
- `tracker.py` reads `GET /equity/positions` by default (`T212_POSITIONS_ENDPOINT`).
- `parse_position()` accepts the current shape and the legacy `/equity/portfolio` shape.
- Current shape: ticker and name from `instrument`, P&L from `walletImpact.unrealizedProfitLoss`,
  value and cost from `walletImpact`. Legacy shape: value and cost are not available.
- Tests: `tests/test_positions.py`.

### Step 1: history sync and CSV import
- `history.py` adds tables `orders_hist`, `fills`, `dividends`, `cash_tx`, `sync_state`,
  plus `summary_snapshots` (raw summary JSON per poll), `positions_latest` and `weight_alerts`.
  Raw JSON is kept in each table.
- Sync pages `/equity/history/{orders,dividends,transactions}` with `limit=50`, following
  `nextPagePath` verbatim. Inserts are `INSERT OR IGNORE`.
- Rate limit: about 3.1s between history calls. On 429 it waits until `x-ratelimit-reset`.
- On 403 it prints the missing scope and skips that account.
- CSV importer matches headers by name. Unknown columns are ignored. Rows it does not
  recognise are listed for manual review. Re-importing the same file adds nothing.
- Tests: `tests/test_history.py` (pagination, 429, 403, idempotent re-sync, alerts, CSV).

### Step 2: analytics (`analytics.py`)
- Exact P&L: realised from sell fills, unrealised from positions, fees and taxes by name.
- Contributions: deposits minus withdrawals. Fees, transfers and interest are shown separately.
  Growth is current value minus net contributions.
- XIRR by bisection (stdlib). TWR chained over stored snapshots, labelled "since tracking started".
- Dividends by month, year, holding and type. Trailing 12 months yield on current value.
- Tax (INVEST only): Section 104 with same-day and 30-day matching, UK tax years 6 April to
  5 April. Compared against Trading 212's `realisedProfitLoss`.
- Trade activity: timeline, running average cost, FIFO holding days, turnover, fees.
- Benchmark comparison from a user-supplied CSV. Skipped with a message if missing.
- Risk: allocation by holding, currency and sector, max drawdown, volatility, worst day.
- `report --month` writes CSV and Markdown. PDF is not produced because `reportlab` is not installed.
- Tests: `tests/test_analytics.py`.

## Approximate or unverified
- Total value field in `/equity/account/summary` is unverified. The default key is
  `totalValue`, with a fallback to `investments.currentValue + totalCash`. Check which applies
  on the first real run.
- Whether `/equity/portfolio` is still served is unverified. Both shapes are supported.
- `walletImpact.realisedProfitLoss` and the sign and meaning of `netValue` on sells are
  unverified. Tax uses `netValue` as proceeds and adds fees to cost. Compare with the CSV export.
- CSV column names and action labels are from memory of the app export. They are matched
  defensively, but check the first real import.
- Tax uses default rates and allowance (18%, 24%, £3,000). These are config values, not checked
  against current rules. Losses are not carried forward and no other income is modelled.
- Time-weighted return and volatility use stored snapshots, not historical prices.
  The first run has little history.
- Dashboard `~` estimates in `dashboard.py` were not changed. You edited that file recently,
  so I left it alone. Exact figures are available from `history.py summary`.
- Non-trade fills (splits, spin-offs, distributions) are not adjusted. They are listed for review.
- `orders_hist` is not updated when an order's status changes after the first sync. Fills
  that appear later are still inserted.
- Duplicate trades are possible if both the API and a CSV import are used for the same account.

## Not verified offline
- Any live API call. Everything was tested with fixtures and mocked `requests.get`.
- Real pagination cursors, real 429 headers, and real 403 messages.
- Output against real history. The copy of `tracker.db` used for the smoke test had no API history yet.
