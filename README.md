# Trading 212 tracker

Polls your Stocks ISA and Invest (general) accounts every minute, stores snapshots
locally (SQLite), shows a live table, and pushes alerts to your phone.
It also writes `dashboard.html`: profit/loss by holding and period, a total P&L chart,
and an hourly view where you pick a day from a calendar to see each stock's gain or loss per hour.

This is a personal monitoring tool. It runs on your own machine, reads your data with
read-only API keys, and never places orders. Your keys, database and reports stay local.

## Run it yourself

### 1. Requirements
- Python 3.9 or newer (`python3 --version`)
- Trading 212 accounts with API access (ISA and/or Invest)
- The free **ntfy** app on your phone for alerts

### 2. Get the code
```
cd investment-tracker
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Create your API keys (read-only)
In the Trading 212 app, for each account (ISA and Invest), create an API key and secret.
Grant read-only scopes: `portfolio`, `history:orders`, `history:dividends`,
`history:transactions` and account data. Never grant order permissions.

### 4. Create your `.env` file
Update a file named `.env` with Trading 212 key and secret.
`.env` file is added in gitignore so it won't be committed automatically.
```
# Trading 212 API keys (read-only). Leave a pair empty to skip that account.
T212_ISA_KEY= <YOUR_KEY>
T212_ISA_SECRET= <YOUR_SECRET>
T212_INVEST_KEY= <YOUR_KEY>
T212_INVEST_SECRET= <YOUR_SECRET>

# Push alerts via ntfy. Use a long random topic name nobody else can guess.
NTFY_TOPIC= <NITFY_TOPIC>

```

### 5. Start the tracker
```
python tracker.py --test-push      # optional: sends one test push to your phone
python tracker.py                  # polls every minute and writes tracker.db + dashboard.html
```
Open `dashboard.html` in any browser. It refreshes every minute while the tracker runs.

### 6. Keep it running (optional)
- macOS: `bash deploy/install_service.sh` installs a background service that starts at login.
  Check it with `launchctl list | grep t212tracker`, stop it with the `launchctl bootout` line it prints.
- Linux: run it under systemd, or `nohup python tracker.py &`.

## Security
- Use read-only API keys. Anyone with the key and secret can see your account; keep them private.
- Anyone who knows your ntfy topic name can read your alerts. Pick a long random name.
- Do not share `tracker.db`, `dashboard.html` or `reports/`. They contain your holdings.

- **Drop alert:** any holding down 10%+ over the last 24h -> urgent push with the
  amount lost in the last hour and since the baseline. Max one per holding per 6h.
- **Hourly digest:** per account, money gained/lost in the last hour and 24h,
  plus the weakest holdings.

### Caveats
- Tax output is only for an information purpose, so don't treat it as an advice.
- This is a monitoring tool, not financial advice. Big daily drops are not always a reason to sell.
- See CHANGELOG.md for what is approximate and what could not be checked offline.
