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

## 2b. Sector Rotation & Relative Strength page

The same site has a second page: **`https://<your-github-username>.github.io/<repo>/rotation.html`**
(also linked from the screener's header). It is built in the same workflow run and uses the same
NSE history, plus NSE's daily file of **all ~190 NSE indices** and the index constituent lists.

| Tab | What it shows |
|---|---|
| **Overview** | Market regime (Nifty 50/500, midcaps vs large caps, VIX), where every sectoral index sits in the rotation, quadrant-change alerts, breadth, RS leaders and "RS new high before price" setups |
| **Relative Rotation Graph** | Animated RRG with tails. Universes: core sectors, all sectoral / thematic / broad / strategy indices, the **stocks of any index** (e.g. Nifty Bank stocks vs Nifty Bank), a custom pick or a watchlist. Any index can be the benchmark. Daily or weekly, adjustable tail and lookback, ▶ play through history |
| **Sector Rotation** | Leadership table (1D…1Y relative returns, rotation score, rank change 1W/4W, daily + weekly quadrant, % of constituents above 50/200 DMA, average RS of members), 13-week rank-history heatmap, Dorsey-Wright-style relative-strength matrix |
| **Sector Leaders** | Pick a sector/theme → its constituents ranked by RS Rating, Mansfield RS and RRG quadrant **vs the sector index**, RS-line new highs, stage, trend template |
| **Relative Strength** | Every NSE stock with institutional RS metrics: **RS Rating 1–99** (IBD method), Mansfield RS, RS-line 52-week high / new-high-before-price, RRG quadrant vs Nifty 500 and vs its sector, Weinstein stage, Minervini trend template, up/down volume, delivery %. Quick screens, filters, CSV export, click a stock for price + RS line + Mansfield charts |
| **Ratio Charts** | Any stock/index ÷ any stock/index (or up to 8 numerators rebased to 100), 50/200-day SMA and RSI of the ratio, 52-week range position, one-click popular ratios (Midcap/Nifty, Equal-weight/Nifty 500, High Beta/Low Vol …) |
| **Market Breadth** | Strike-style diffusion indicators for Nifty 50/100/200/500, Midcap 150, Smallcap 250, Microcap 250, F&O, all NSE and every sector: % above 20/50/100/200 SMA, % RSI ≥ 50, % MACD buy, % ADX ≥ 25, % RMI > 50, new 1M/3M/52W highs & lows, A/D line, McClellan Oscillator & Summation Index, Zweig breadth thrust, 4% movers, sector participation table |
| **Heatmap** | Treemap of any universe sized by market cap, coloured by 1D…6M return, RS Rating or Mansfield RS (colour-blind-safe option) |
| **Methodology** | Every formula and data source |

**Watchlists** (★ in the header) are saved in your browser and can be used as a universe in the RRG,
Relative Strength, Heatmap and Ratio tabs.

Notes
- The RRG uses a public smoothed approximation of the proprietary JdK RS-Ratio / RS-Momentum; the
  quadrants and rotation behave the same, exact values can differ a little from Strike/StockCharts.
- Strike's "RMI" diffusion uses its proprietary Rohit Momentum Indicator. This page uses Roger
  Altman's published Relative Momentum Index (RMI 20, 5) instead.
- During market hours, indices that TradingView doesn't carry are **estimated** from their
  constituents until NSE's official close arrives (the header shows how many).
- Breadth history uses today's index constituents (survivorship bias).
- The store now keeps NSE history from **1 Jan two years back**. The first run after this update
  back-fills that extra year once (about 5 extra minutes).

## 2c. Smart Money & Institutional page

The third page, **`https://<your-github-username>.github.io/<repo>/smartmoney.html`** (linked from both other
headers), shows where large, delivery-backed money is active. It is built in the same workflow run from NSE's
own files: delivery (`sec_bhavdata_full`), the F&O bhavcopy, participant-wise open interest, bulk/block deals
and FII/DII figures.

| Tab | What it shows |
|---|---|
| **Dashboard** | Smart-money breadth (buy-side vs sell-side footprints under the Nifty), spikes, accumulation vs distribution, FII long %, FII/DII cash, top footprints, gradual-inflow streaks, strongest accumulation, setups that fired, live heavy-volume movers, sector smart-money map |
| **SMF Sheet** | The SMF-style grid: every stock × the last 10–60 sessions, coloured like the demo sheet (red < 3 < green, ≥ 8 bold) with ▲/▼ buy/sell-bias marks, sortable by any day, CSV export in the same layout |
| **SMF Screener** | Presets (SMF ≥ 3.5, gradual inflow, spike ≥ 8, buy/sell-side footprints, retest of SMF zone, quiet delivery accumulation, long build-up, institutional deals, live RVOL …) plus min/max filters on ~18 metrics |
| **Institutional Accumulation** | Institutional Score 0–100 (delivery, footprint balance, money flow, trend/RS, F&O OI + deals) with components, Wyckoff phase map now vs 20 sessions ago, score jumps |
| **Market Setups** | 18 rule-based setups (SMF inflow/spike/footprints, absorption, SMF-zone retest, pocket pivot, delivery breakout, quiet accumulation, Wyckoff spring/upthrust, VCP, episodic pivot, NR7, F&O build-ups, institutional deal buying) with today's signals and their historical hit rates |
| **Post-move Analysis** | For every setup and SMF bucket: average return after 1/3/5/10/20 sessions, win rate, MFE/MAE, "+10% before −5%", edge vs the all-stock baseline; recent signals and what happened since |
| **F&O & Deals** | Long/short build-up, short covering, long unwinding (expiry-adjusted OI), PCR; bulk & block deals classified by client type with intraday round-trips removed, net institutional value by stock |
| **FII / DII** | FII index-futures long %, net index futures by participant, participant OI table, FII/DII cash, Nifty/Bank Nifty PCR |
| **Methodology** | Every formula and source |

Click any stock for a chart with candles, SMF bars, delivery %, volume and futures OI, its SMF zones, setup
history with outcomes, deals, and a **Copy SMF zones (Pine)** button that draws the zones on your TradingView chart.

**SMF score.** `SMF = 3.13 × (DPT ÷ 20-day average DPT)^0.571`, with DPT = deliverable quantity ÷ number of
trades. Fitted to the public SMF demo sheet (Mar–Apr 2025) against NSE's delivery files: R² = 0.945 on 5,366
stock-days. Same scale: ≈3 normal, ≥ 3.5 favourable, rising 3.5 → 4 → 4.5 = gradual inflow, ≥ 8 sharp move.

Notes
- NSE publishes delivery only after the close, so SMF scores update with the 18:15 run. During market hours the
  page shows live price, time-of-day-adjusted relative volume and provisional price/volume setups.
- The first run back-fills about two years of delivery files and the last ~260 sessions of F&O and participant
  files (a few extra minutes; F&O completes over two runs). Deal history back-fill uses NSE's report API and
  fills a little each run where NSE allows it; FII/DII cash history starts on the day the page is installed.

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
| Delivery qty / %, VWAP, trades (smart-money page) | NSE `sec_bhavdata_full` |
| Futures & options OI | NSE F&O UDiFF bhavcopy, `/content/fo/BhavCopy_NSE_FO_…` |
| Participant-wise OI | NSE `/content/nsccl/fao_participant_oi_DDMMYYYY.csv` |
| Bulk / block deals | NSE `/content/equities/bulk.csv`, `block.csv` (+ report API back-fill) |
| FII / DII cash | NSE provisional figures (www.nseindia.com API, best effort) |

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
python scripts/build_rotation.py      # NSE index files + build the rotation page data
python scripts/build_smartmoney.py    # delivery, F&O, deals + build the smart-money page data
# then open site/index.html, site/rotation.html or site/smartmoney.html in a browser
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
| Smart-money page says *smartmoney-data.js was not found* | The workflow hasn't run since you added the page, or its *Build the Smart Money* step failed (open the run log). |
| Rotation page says *rotation-data.js was not found* | The workflow hasn't run since you added the page, or its *Build the Sector Rotation* step failed (open the run log). The screener is unaffected. |
| Scheduled runs stopped | GitHub pauses schedules after 60 days without commits. The daily data commits normally prevent this. If it happens, click *Enable workflow* in Actions. |

---

## 6. Project layout

```
.github/workflows/update.yml   schedule + build + publish
scripts/nse_data.py            NSE downloads and the daily file store (data/eod/YYYY/YYYYMMDD.csv.gz)
scripts/fundamentals.py        TradingView fundamentals (one request)
scripts/build_data.py          adjustments, periods, indicators → site/data.js
scripts/nse_indices.py         NSE daily index file store (data/indices/) + index constituents
scripts/build_rotation.py      RS Rating, Mansfield, RRG, stages, breadth → site/rotation-data.js,
                               site/rotation-series.js, site/breadth-data.js
site/index.html                the screener (single file, no external libraries)
site/rotation.html             Sector Rotation & Relative Strength page
scripts/nse_smartmoney.py      NSE delivery / F&O / participant OI / deals / FII-DII downloads (data/sm/)
scripts/build_smartmoney.py    SMF score, Institutional Score, setups, post-move stats → site/smartmoney-data.js,
                               site/sm-hist/ (per-stock history, loaded on demand)
site/smartmoney.html           Smart Money & Institutional page
tests/sm_fixture_server.py     local stand-in for NSE/TradingView to test the smart-money build offline
site/vendor/echarts.min.js     Apache ECharts 5.6 (charting library used by rotation.html)
data/                          stored NSE history (kept from 1 Jan two years back; older files pruned)
tests/                         synthetic fixtures + checks (python tests/make_fixtures.py, etc.)
run_local.bat                  optional Windows launcher
workflow-update.yml            copy of the workflow, for pasting into GitHub if needed
```

*For research only. Not investment advice. Check levels on your own chart before trading.*
