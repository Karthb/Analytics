# NSE Camarilla Screener

A free web screener for **all NSE stocks**. It refreshes itself from NSE every day. You can filter by
**Camarilla pivot levels** (R2 / R3 / R4 / R5, S-levels, any period) together with **Screener.in-style
fundamentals** (P/E, ROCE, ROE, growth, debt, Piotroski …).

It runs on GitHub's free services. No server, no subscription, nothing to install. Open the link on
any laptop or phone with internet and the newest data loads.

```
NSE official files ──► GitHub Action (daily after close + every 30 min in market hours)
                         • downloads new bhavcopy days, adjusts splits/bonuses
                         • pulls fundamentals for every NSE stock in one request
                         • writes site/data.js
                                   │
                                   ▼
                     GitHub Pages website  ──►  you open the link → latest data loads
```

---

## 1. One-time setup (about 10 minutes)

1. **Create a free GitHub account** at <https://github.com/signup> (skip this if you already have one).
2. **Create a repository:** click **+ → New repository**.
   - Name: `nse-screener` (any name works).
   - Visibility: **Public**. GitHub Pages and Actions are free for public repos. The repo only holds public market data.
   - Tick **Add a README file** → **Create repository**.
3. **Turn on Pages:** repo **Settings → Pages → Build and deployment → Source: _GitHub Actions_**.
4. **Allow the robot to save data:** **Settings → Actions → General → Workflow permissions →
   _Read and write permissions_ → Save**.
5. **Upload this project:** on the repo's **Code** tab click **Add file → Upload files**. Drag in
   *everything inside* this folder (`.github`, `scripts`, `site`, `tests`, `requirements.txt`, `run_local.bat`,
   `README.md` …), then click **Commit changes**.
   > **The workflow file must sit at `.github/workflows/update.yml`.** If the `.github` folder didn't
   > upload (or you're working from the unzipped copy in your Analytics project folder, which doesn't have
   > it), click **Add file → Create new file**. Type the name `.github/workflows/update.yml`, paste in the
   > contents of **`workflow-update.yml`** (an identical copy), and commit.
6. **First run:** the upload starts the workflow automatically. To start it by hand, open
   **Actions → Update screener data → Run workflow**. The first run back-fills about 2 years of NSE
   history and takes **10–15 minutes**. Later runs take 1–2 minutes.
7. **Open your screener:** `https://<your-github-username>.github.io/nse-screener/`. The link is also
   under **Settings → Pages**. Bookmark it on your laptop and phone.

After setup it maintains itself. The schedule (IST, Mon–Fri):

| Time | What happens |
|---|---|
| 09:45 – 15:15, every 30 min | Live prices refreshed (TradingView, ≈15-min delayed) |
| 15:35 | Closing prices |
| 18:15, 20:00, 22:00 | Official NSE bhavcopy for the day (retries if NSE publishes late) |
| 08:00 daily | Safety run, also picks up special weekend sessions (Budget day, Muhurat) |

To refresh by hand at any time, use **Actions → Update screener data → Run workflow**. The page's
**↻ Reload data** button picks up the newest file without leaving the page.

---

## 2. Using the screener

**Quick screens** (one click): Above R4 (breakout) · Between R3 & R4 · Between R2 & R3 · Near R3/R4 (±2%) ·
Crossed above R4/R3 in last 5 days · Touched R4 this period · Above R5 · Between S3 & S4 · Below S3/S4.

**Pivot levels panel**
- **Type:** Camarilla (default), Traditional, Classic, Fibonacci, Woodie, DM. These are the same formulas as
  TradingView's *Pivot Points Standard*.
- **Period:** Yearly (default), Quarterly, Monthly, Weekly, Daily.
  *Your TradingView chart is on 1W with pivots on "Auto". On a weekly chart, Auto means **Yearly** levels,
  which is why the default here is Yearly.*
- **Levels for:** *Current period* is exactly what the chart shows. *Next period* builds developing levels
  from the period in progress, for example tomorrow's daily levels after today's close.
- **Conditions:** build any rule you like, and combine several with *Match all / Any*.
  `[Price | Period high | Period low | Period open] [above | below | between | within ±% of | crossed above | crossed below] [R1…R5, S1…S5, P, prev-period High/Low/Close]`

**Universe:** search, Nifty index (50 / Next 50 / 100 / 200 / 500 / Midcap 150 / Smallcap 250 /
Microcap 250 / outside Nifty 500), sector, F&O only, SME on/off, BE/BZ series on/off.

**Fundamentals & technicals:** each metric has a min/max box. Leave a box blank to ignore it.

**Table:** click a header to sort. Use the level chips (R5 … PL) to choose which levels appear as columns.
*Δ% vs* shows the distance from any level. **Columns** adds or removes any of the ~60 metrics, and
**Export CSV** opens the list in Excel.

**Stock detail:** click any row. You get a Screener.in-style ratio card, the full level table for all
5 periods (nearest level highlighted), and links to Screener.in, the TradingView chart and NSE.

**Saving screens:** the page address holds all your filters, so bookmark it to keep a screen. Your
last-used screen also comes back automatically.

---

## 3. Where the numbers come from

| Data | Source |
|---|---|
| Daily OHLC, volume, previous close, ISIN | NSE UDiFF CM bhavcopy, `nsearchives.nseindia.com/content/cm/…` |
| Shares outstanding (→ market cap, split/bonus detection) | NSE PR bundle, MCAP file |
| Delivery % | NSE `sec_bhavdata_full` |
| Stock list, face value, listing date | NSE `EQUITY_L.csv` |
| Symbol renames | NSE `symbolchange.csv` |
| Index membership, F&O list | NSE index constituent CSVs, `fo_mktlots.csv` |
| Fundamentals (P/E inputs, ROCE, ROE, growth, D/E, Piotroski, shareholders …) | TradingView screener feed: one request covers all NSE stocks (unofficial endpoint) |
| Intraday prices (market hours only) | TradingView screener feed, ≈15-min delayed |

**Camarilla formulas** (previous period H, L, C):
`R1/S1 = C ± 1.1(H−L)/12 · R2/S2 = C ± 1.1(H−L)/6 · R3/S3 = C ± 1.1(H−L)/4 · R4/S4 = C ± 1.1(H−L)/2 ·
R5 = (H/L)·C · S5 = C − (R5 − C) · P = (H+L+C)/3`

**Checked against your chart (TIPSMUSIC, yearly 2026 levels).** NSE's 2025 bhavcopies give H = 760.50,
L = 483.00 and C = 552.25. From those, R3 = **628.56**, R4 = **704.88** and S4 = **399.63**. TradingView shows
628.55 / 704.90 / 399.65 because it rounds to the ₹0.05 tick. The monthly R3 for Nov 2026 (835.056) matches
TradingView's own value exactly.

**Splits & bonuses:** NSE's daily file does *not* adjust the previous close for splits or bonuses. The
build detects them from the jump in NSE's share count and back-adjusts history the way TradingView
does. Example: Bajaj Finance's 1:2 split + 4:1 bonus on 16-Jun-2025 becomes a ×0.1 adjustment. The stock
detail panel lists every adjustment applied.

**Screener.in metrics:** almost all have a direct equivalent (see the detail panel). Three don't, because
no free source publishes them: **pledged %**, **3/5-year EPS growth** and **ROE 5-yr variance**. Values
come from TradingView's definitions, so they can differ slightly from Screener.in's. For example,
TIPSMUSIC ROCE shows 111.8 % here and 118 % on Screener.in.

---

## 4. Optional: run it on your own PC

Install Python 3.10+ from python.org (tick *Add python.exe to PATH*), then double-click
**`run_local.bat`**. It downloads only the days you don't have yet and then opens the screener in your
browser. This is handy as a backup if GitHub is ever unavailable.

Commands for other systems:
```bash
pip install -r requirements.txt
python scripts/build_data.py          # download new NSE days + build site/data.js
# then open site/index.html in a browser
```
Useful flags: `--no-download` (rebuild offline), `--no-fundamentals`, `--no-live`, `--start 2025-01-01`.

---

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| Page says *Data not available yet* | The first workflow run hasn't finished. Check the **Actions** tab. |
| Workflow fails at *Save new NSE data* with 403 | Step 4 above: set workflow permissions to *Read and write*. |
| Workflow fails at *deploy* | Step 3 above: set Pages source to *GitHub Actions*, then re-run. |
| Banner: *Fundamentals unavailable* | The TradingView feed didn't answer that run. Prices and levels still work, and the next run usually fixes it. |
| Data stuck on an old date | NSE may not have published yet (holiday or late). Runs retry at 20:00 and 22:00. You can also click *Run workflow*. |
| NSE blocks GitHub's servers (download errors in the log) | Use `run_local.bat` on your PC, which uses your own Indian internet connection. |
| Scheduled runs stopped | GitHub pauses schedules after 60 days without commits. The daily data commits normally prevent this. If it happens, click *Enable workflow* in Actions. |

---

## 6. Project layout

```
.github/workflows/update.yml   schedule + build + publish
scripts/nse_data.py            NSE downloads and the daily file store (data/eod/YYYY/YYYYMMDD.csv.gz)
scripts/fundamentals.py        TradingView fundamentals (one request)
scripts/build_data.py          adjustments, periods, indicators → site/data.js
site/index.html                the screener (single file, no external libraries)
data/                          stored NSE history (kept from 1 Jan of last year; older files pruned)
tests/                         synthetic fixtures + checks (python tests/make_fixtures.py, etc.)
run_local.bat                  optional Windows launcher
workflow-update.yml            copy of the workflow, for pasting into GitHub if needed
```

*For research only. Not investment advice. Check levels on your own chart before trading.*
