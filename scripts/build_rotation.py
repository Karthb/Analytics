"""
Build the data files for the Sector Rotation & Relative Strength page (site/rotation.html).

    python scripts/build_rotation.py                 # download new NSE index files, then build
    python scripts/build_rotation.py --no-download   # rebuild from stored data only

Writes three JavaScript files next to the page:
  site/rotation-data.js  index closes (all NSE equity indices), constituents, per-stock RS metrics
  site/rotation-series.js daily adjusted closes of ~800 widely followed stocks (RRG / ratio charts)
  site/breadth-data.js   market breadth & diffusion indicators for ~45 universes

Relative-strength methods implemented (see the page's Methodology panel for details):
  * RS Rating 1-99 (IBD / O'Neil style): weighted 3/6/9/12-month performance, percentile vs all NSE stocks
  * Mansfield RS (Stan Weinstein): weekly RS line vs its 52-week average, in %
  * RS line new 52-week high, and "RS new high before price" (O'Neil blue dot)
  * Relative Rotation Graph quadrant (JdK-style RS-Ratio / RS-Momentum, smoothed public approximation)
  * Weinstein stage (30-week MA), Minervini trend template (8 rules)
  * Up/Down volume ratio (50 days), delivery %
"""
from __future__ import annotations

import argparse
import datetime as dt
import warnings
import json
import math
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_data as bd  # noqa: E402
import fundamentals as fnd  # noqa: E402
import nse_data as nse  # noqa: E402
import nse_indices as ni  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")
SME_SERIES = {"SM", "ST"}
BREADTH_SESSIONS = 500        # history kept in breadth-data.js
SERIES_SESSIONS = 500         # history kept in rotation-series.js
RRG_N_WEEKLY = 10             # must match the page's default weekly lookback
WEEKS_RS_HISTORY = 52

BROAD_UNIVERSES = [  # (code, label, index code in members)
    ("NIFTY50", "Nifty 50"), ("NIFTYNEXT50", "Nifty Next 50"), ("NIFTY100", "Nifty 100"),
    ("NIFTY200", "Nifty 200"), ("NIFTY500", "Nifty 500"), ("NIFTYMIDCAP150", "Nifty Midcap 150"),
    ("NIFTYSMALLCAP250", "Nifty Smallcap 250"), ("NIFTYMICROCAP250", "Nifty Microcap 250"),
]
OLD_BROAD_KEYS = {"N50": "NIFTY50", "NN50": "NIFTYNEXT50", "N100": "NIFTY100", "N200": "NIFTY200",
                  "N500": "NIFTY500", "MID150": "NIFTYMIDCAP150", "SML250": "NIFTYSMALLCAP250",
                  "MICRO250": "NIFTYMICROCAP250"}

# NSE macro industry (index constituent files) -> preferred sector index, in order of preference
NSE_INDUSTRY_TO_INDEX = {
    "Financial Services": ["NIFTYFINANCIALSERVICES"],
    "Information Technology": ["NIFTYIT"],
    "Healthcare": ["NIFTYHEALTHCARE", "NIFTYPHARMA"],
    "Automobile and Auto Components": ["NIFTYAUTO"],
    "Fast Moving Consumer Goods": ["NIFTYFMCG"],
    "Metals & Mining": ["NIFTYMETAL"],
    "Oil Gas & Consumable Fuels": ["NIFTYOILGAS", "NIFTYENERGY"],
    "Power": ["NIFTYPOWER", "NIFTYENERGY"],
    "Realty": ["NIFTYREALTY"],
    "Media Entertainment & Publication": ["NIFTYMEDIA"],
    "Consumer Durables": ["NIFTYCONSUMERDURABLES"],
    "Chemicals": ["NIFTYCHEMICALS", "NIFTYCOMMODITIES"],
    "Capital Goods": ["NIFTYCAPITALGOODS", "NIFTYINDIAMANUFACTURING", "NIFTYINFRASTRUCTURE"],
    "Construction": ["NIFTYCONSTRUCTION", "NIFTYINFRASTRUCTURE"],
    "Construction Materials": ["NIFTYCEMENT", "NIFTYCOMMODITIES"],
    "Telecommunication": ["NIFTYTELECOMMUNICATIONS", "NIFTYINDIADIGITAL", "NIFTYSERVICESSECTOR"],
    "Consumer Services": ["NIFTYCONSUMERSERVICES", "NIFTYINDIACONSUMPTION"],
    "Services": ["NIFTYSERVICESSECTOR"],
    "Textiles": ["NIFTYINDIACONSUMPTION"],
}
# TradingView sector / industry -> sector index (for stocks outside NSE's index files)
TV_INDUSTRY_TO_INDEX = {
    "Motor Vehicles": ["NIFTYAUTO"], "Auto Parts: OEM": ["NIFTYAUTO"], "Automotive Aftermarket": ["NIFTYAUTO"],
    "Real Estate Development": ["NIFTYREALTY"], "Real Estate Investment Trusts": ["NIFTYREALTY"],
    "Major Banks": ["NIFTYBANK"], "Regional Banks": ["NIFTYBANK"],
    "Construction Materials": ["NIFTYCEMENT", "NIFTYCOMMODITIES"],
    "Broadcasting": ["NIFTYMEDIA"], "Movies/Entertainment": ["NIFTYMEDIA"], "Publishing: Newspapers": ["NIFTYMEDIA"],
    "Cable/Satellite TV": ["NIFTYMEDIA"], "Advertising/Marketing Services": ["NIFTYMEDIA"],
    "Pharmaceuticals: Major": ["NIFTYPHARMA"], "Pharmaceuticals: Other": ["NIFTYPHARMA"],
    "Pharmaceuticals: Generic": ["NIFTYPHARMA"],
}
TV_SECTOR_TO_INDEX = {
    "Finance": ["NIFTYFINANCIALSERVICES"], "Technology Services": ["NIFTYIT"],
    "Health Technology": ["NIFTYPHARMA", "NIFTYHEALTHCARE"], "Health Services": ["NIFTYHEALTHCARE"],
    "Consumer Durables": ["NIFTYCONSUMERDURABLES"], "Non-Energy Minerals": ["NIFTYMETAL"],
    "Energy Minerals": ["NIFTYOILGAS", "NIFTYENERGY"], "Utilities": ["NIFTYPOWER", "NIFTYENERGY"],
    "Process Industries": ["NIFTYCHEMICALS", "NIFTYCOMMODITIES"],
    "Producer Manufacturing": ["NIFTYCAPITALGOODS", "NIFTYINDIAMANUFACTURING"],
    "Industrial Services": ["NIFTYCONSTRUCTION", "NIFTYINFRASTRUCTURE"],
    "Consumer Non-Durables": ["NIFTYFMCG"], "Retail Trade": ["NIFTYRETAIL", "NIFTYINDIACONSUMPTION"],
    "Transportation": ["NIFTYTRANSPORTATIONLOGISTICS"], "Communications": ["NIFTYTELECOMMUNICATIONS", "NIFTYINDIADIGITAL"],
    "Electronic Technology": ["NIFTYINDIAMANUFACTURING", "NIFTYCAPITALGOODS"],
    "Consumer Services": ["NIFTYCONSUMERSERVICES", "NIFTYINDIACONSUMPTION"],
    "Commercial Services": ["NIFTYSERVICESSECTOR"], "Distribution Services": ["NIFTYSERVICESSECTOR"],
}


def log(msg: str) -> None:
    print(f"[rotation] {msg}", flush=True)


# --------------------------------------------------------------------------- numeric helpers
def sig(x, digits=5):
    """Round to `digits` significant figures (keeps the JS files small)."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    if f == 0:
        return 0
    nd = max(0, digits - int(math.floor(math.log10(abs(f)))) - 1)
    v = round(f, nd)
    return int(v) if nd == 0 else v


def rnd(x, nd=2):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def shift(a: np.ndarray, k: int) -> np.ndarray:
    """Shift a (T x N) array down by k rows (NaN fill)."""
    out = np.full_like(a, np.nan, dtype=float)
    if k == 0:
        return a.astype(float, copy=True)
    if k < a.shape[0]:
        out[k:] = a[:-k]
    return out


def wma(a: np.ndarray, n: int) -> np.ndarray:
    """Linear weighted moving average along axis 0 (NaN if any value in the window is NaN)."""
    acc = np.zeros_like(a, dtype=float)
    for i in range(n):
        acc = acc + (n - i) * shift(a, i)
    return acc / (n * (n + 1) / 2)


def sma(a: np.ndarray, n: int, min_periods: int | None = None) -> np.ndarray:
    return pd.DataFrame(a).rolling(n, min_periods=min_periods or n).mean().to_numpy()


def ewm(a: np.ndarray, **kw) -> np.ndarray:
    return pd.DataFrame(a).ewm(adjust=False, **kw).mean().to_numpy()


def rrg(price: np.ndarray, bench: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """JdK-style RS-Ratio / RS-Momentum (public smoothed approximation; identical to the page's JS).
        rs        = price / benchmark
        RS-Ratio  = 100 * WMA( rs / WMA(rs, n), n )
        RS-Mom    = 100 * RS-Ratio / WMA(RS-Ratio, n)
    """
    b = bench.reshape(-1, 1) if bench.ndim == 1 else bench
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = price / b
        ratio = 100 * wma(rs / wma(rs, n), n)
        mom = 100 * ratio / wma(ratio, n)
    return ratio, mom


def quadrant(r, m) -> str | None:
    if r is None or m is None or not (math.isfinite(r) and math.isfinite(m)):
        return None
    if r >= 100:
        return "L" if m >= 100 else "W"
    return "I" if m >= 100 else "G"


def week_last_rows(dates: pd.DatetimeIndex) -> np.ndarray:
    """Row index of the last session of every Monday-start week (the current week included)."""
    d = dates.to_numpy().astype("datetime64[D]").astype(np.int64)
    wk = d - (d + 3) % 7
    last = np.r_[wk[1:] != wk[:-1], True]
    return np.flatnonzero(last)


# --------------------------------------------------------------------------- indicators on (T x N) matrices
def rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(c, axis=0, prepend=np.nan)
    up = ewm(np.where(np.isnan(d), np.nan, np.clip(d, 0, None)), alpha=1 / n)
    dn = ewm(np.where(np.isnan(d), np.nan, np.clip(-d, 0, None)), alpha=1 / n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(dn == 0, 100.0, 100 - 100 / (1 + up / dn))


def rmi(c: np.ndarray, n: int = 20, m: int = 5) -> np.ndarray:
    """Altman's Relative Momentum Index (S&C, Feb 1993): RSI of m-bar momentum, Wilder smoothing n."""
    d = c - shift(c, m)
    up = ewm(np.where(np.isnan(d), np.nan, np.clip(d, 0, None)), alpha=1 / n)
    dn = ewm(np.where(np.isnan(d), np.nan, np.clip(-d, 0, None)), alpha=1 / n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where((up + dn) == 0, 50.0, 100 * up / (up + dn))


def macd_buy(c: np.ndarray) -> np.ndarray:
    line = ewm(c, span=12) - ewm(c, span=26)
    sig_ = ewm(line, span=9)
    return line > sig_


def adx(h: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    pc, ph, pl = shift(c, 1), shift(h, 1), shift(lo, 1)
    tr = np.fmax(h - lo, np.fmax(np.abs(h - pc), np.abs(lo - pc)))
    upm, dnm = h - ph, pl - lo
    pdm = np.where((upm > dnm) & (upm > 0), upm, 0.0)
    ndm = np.where((dnm > upm) & (dnm > 0), dnm, 0.0)
    nan = np.isnan(tr)
    pdm[nan] = np.nan
    ndm[nan] = np.nan
    atr = ewm(tr, alpha=1 / n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * ewm(pdm, alpha=1 / n) / atr
        ndi = 100 * ewm(ndm, alpha=1 / n) / atr
        dx = 100 * np.abs(pdi - ndi) / (pdi + ndi)
    return ewm(dx, alpha=1 / n)


def bars_count(c: np.ndarray) -> np.ndarray:
    return np.cumsum(~np.isnan(c), axis=0)


# --------------------------------------------------------------------------- TradingView index quotes
def fetch_tv_indices(session) -> dict[str, dict]:
    tickers = [f"NSE:{t}" for t in ni.TV_INDEX.values()]
    payload = {"symbols": {"tickers": tickers, "query": {"types": []}},
               "columns": ["name", "open", "high", "low", "close", "change"]}
    try:
        r = session.post(fnd.TV_SCAN_URL, json=payload, timeout=30,
                         headers={"Content-Type": "application/json", "Origin": "https://in.tradingview.com",
                                  "Referer": "https://in.tradingview.com/"})
        r.raise_for_status()
        rows = r.json().get("data") or []
    except Exception as exc:
        log(f"TradingView index quotes failed: {exc}")
        return {}
    by_tv = {v: k for k, v in ni.TV_INDEX.items()}
    out = {}
    for item in rows:
        t = str(item.get("s", "")).split(":", 1)[-1]
        d = item.get("d") or []
        if t in by_tv and len(d) >= 6:
            out[by_tv[t]] = {"open": d[1], "high": d[2], "low": d[3], "close": d[4], "change": d[5]}
    log(f"TradingView index quotes: {len(out)}")
    return out


# --------------------------------------------------------------------------- main build
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--site", default=str(ROOT / "site"))
    ap.add_argument("--now", help="pretend current IST time, e.g. 2026-11-20T20:00 (testing)")
    ap.add_argument("--no-download", action="store_true", help="do not contact NSE / TradingView")
    ap.add_argument("--no-live", action="store_true", help="never add provisional intraday bars")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    now = dt.datetime.fromisoformat(args.now).replace(tzinfo=IST) if args.now else dt.datetime.now(IST)
    today = now.date()
    keep_from = dt.date(today.year - 2, 1, 1)
    data_dir = Path(args.data)
    store = nse.EodStore(data_dir)
    istore = ni.IndexStore(data_dir)
    session = nse.make_session()

    # ---- 1. NSE index files for every stored stock session
    sessions = sorted(d for d in store.existing_dates() if d >= keep_from)
    if not args.no_download:
        ni.update_index_store(istore, sessions, session, workers=args.workers)
        istore.prune(keep_from)
        members_doc = ni.update_members(store.meta, session, now)
    else:
        members_doc = ni.load_members(store.meta)

    # ---- 2. adjusted stock history (same pipeline as the screener)
    ref = bd.load_reference(session, store.meta, offline=True)
    raw = store.load(keep_from)
    if raw.empty:
        log("ERROR: no NSE stock data in the store")
        return 1
    raw = nse.remove_duplicate_days(store, raw)
    have = set(raw["date"].dt.date)
    no_sess = store.no_session_dates()
    unknown = [d for d in pd.bdate_range(raw["date"].min(), raw["date"].max()).date
               if d not in have and d not in no_sess]
    nse_date = raw["date"].max()
    df = bd.apply_symbol_changes(raw, ref["symchg"])
    df = bd.link_by_isin(df)
    df["_p"] = df["series"].map(nse.SERIES_PRIORITY).fillna(9)
    df = df.sort_values(["symbol", "date", "_p"]).drop_duplicates(["symbol", "date"], keep="first").drop(columns="_p")
    df, _events = bd.adjust_corporate_actions(df, unknown, bd.load_ca_notices(store.meta))

    tv = None if args.no_download else fnd.fetch_tradingview(session)
    tv = load_or_cache_tv_classes(store.meta, tv)
    live = None
    if not args.no_live and not args.no_download and tv is not None and "tv_close" in tv.columns:
        df, live = bd.add_live_bars(df, tv, now)
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    latest = df["date"].max()
    log(f"stocks: {df['symbol'].nunique()} symbols, {df['date'].nunique()} sessions "
        f"{df['date'].min().date()} .. {latest.date()}" + (f" (live {live['time']})" if live else ""))

    # ---- 3. universe (same rules as the screener)
    eq = ref["equity"]
    eq_syms = set(eq["symbol"]) if eq is not None else set()
    tv_type = dict(zip(tv["tv"], tv["tv_type"])) if tv is not None and "tv_type" in tv.columns else {}
    last_seen = df.groupby("symbol")["date"].max()
    series_last = df.groupby("symbol")["series"].last()
    keep = []
    for sym, last_d in last_seen.items():
        tt = tv_type.get(fnd.tv_key(sym))
        if re.search(r"-RE\d*$", sym) or tt == "fund":
            continue
        if last_d < latest - pd.Timedelta(days=60):
            continue
        if sym in eq_syms or series_last[sym] in SME_SERIES or tt == "stock":
            keep.append(sym)
    df = df[df["symbol"].isin(set(keep))]
    syms = sorted(keep)
    dates = pd.DatetimeIndex(sorted(df["date"].unique()))
    T, N = len(dates), len(syms)
    log(f"universe: {N} stocks x {T} sessions")

    def wide(col):
        return (df.pivot(index="date", columns="symbol", values=col)
                .reindex(index=dates, columns=syms).to_numpy(dtype=float))

    C, H, L, V, VAL = wide("close"), wide("high"), wide("low"), wide("volume"), wide("value")
    ISS = pd.DataFrame(wide("issue_size")).ffill().bfill().to_numpy()
    DEL = wide("deliv_pct")
    traded = ~np.isnan(C)
    Cf = pd.DataFrame(C).ffill(limit=5).to_numpy()
    Hf = np.where(traded, H, Cf)
    Lf = np.where(traded, L, Cf)
    alive = pd.DataFrame(traded.astype(float)).rolling(10, min_periods=1).max().to_numpy() > 0
    nbars = bars_count(C)
    sme = np.array([series_last.get(s) in SME_SERIES for s in syms])
    col_of = {s: i for i, s in enumerate(syms)}

    # ---- 4. index matrix aligned to stock sessions (+ estimates for sessions NSE hasn't published yet)
    ix = istore.load(keep_from)
    ix = ix[ix["name"].map(ni.index_category).notna()]
    members, member_src = build_members(members_doc, ref, syms)
    idx_codes, idx_meta, IC, live_idx = build_index_matrix(
        ix, dates, members, Cf, C, ISS, col_of, tv, session if (live and not args.no_download) else None)

    # ---- 5. stock-level relative strength metrics
    def icol(code):
        return IC[:, idx_codes.index(code)] if code in idx_codes else None

    b50, b500 = icol("NIFTY50"), icol("NIFTY500")
    if b500 is None:
        b500 = b50
    sec_idx = assign_sector_index(syms, members, members_doc.get("industry", {}), ref, tv, set(idx_codes))
    metrics, rs_hist, week_rows = stock_metrics(
        syms, dates, C, Cf, Hf, Lf, V, VAL, ISS, DEL, traded, nbars, sme, b50, b500, IC, idx_codes, sec_idx)

    # ---- 6. breadth / diffusion indicators
    universes = [(c, lbl, [col_of[s] for s in members.get(c, []) if s in col_of]) for c, lbl in BROAD_UNIVERSES]
    fo = [col_of[s] for s in ref["fo"] if s in col_of] if ref.get("fo") else []
    universes.append(("FO", "F&O stocks", fo))
    universes.append(("ALL", "All NSE (main board)", [i for i in range(N) if not sme[i]]))
    lbl_of = dict(zip(idx_codes, [m["label"] for m in idx_meta]))
    cat_of = dict(zip(idx_codes, [m["cat"] for m in idx_meta]))
    for code in sorted(members, key=lambda c: (cat_of.get(c, "z") != "sector", lbl_of.get(c, c))):
        if code in dict(BROAD_UNIVERSES) or cat_of.get(code) not in ("sector", "thematic"):
            continue
        cols = [col_of[s] for s in members[code] if s in col_of]
        if len(cols) >= 5:
            universes.append((code, lbl_of.get(code, code), cols))
    breadth = breadth_series(dates, C, Cf, Hf, Lf, traded, alive, nbars, universes)

    # ---- 7. write files
    site = Path(args.site)
    site.mkdir(parents=True, exist_ok=True)
    gen = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    stale_note = None
    if live_idx.get("estimated") or live_idx.get("stale"):
        stale_note = live_idx
    names = ref["names"]
    eq_name = dict(zip(eq["symbol"], eq["company"])) if eq is not None else {}
    tvd = tv.set_index("tv") if tv is not None else pd.DataFrame()

    stock_cols = ["sym", "name", "sector", "industry", "nse_ind", "sec", "idx", "fo", "sme", "close", "chg",
                  "r1w", "r1m", "r3m", "r6m", "r1y", "rs", "rs1w", "rs4w", "rs13w", "rs_part",
                  "mrs50", "mrs500", "mrs_sec", "rsnh", "rsnh_bp", "rsnh5", "q500", "q500p", "qsec", "qsecp",
                  "stage", "tt", "off_hi", "above_lo", "udv", "val_cr", "mcap", "deliv", "bars"]
    fo_set = set(ref.get("fo") or [])
    idx_tags = {}
    for code, _ in BROAD_UNIVERSES:
        for s in members.get(code, []):
            idx_tags.setdefault(s, []).append(code)
    nse_ind = {**ref["index"][1], **members_doc.get("industry", {})} if ref.get("index") else members_doc.get("industry", {})
    rows = []
    for i, s in enumerate(syms):
        m = metrics[i]
        if m is None:
            continue
        t = tvd.loc[fnd.tv_key(s)] if fnd.tv_key(s) in tvd.index else None
        tg = (lambda k: None if t is None or k not in t.index or pd.isna(t[k]) else t[k])  # noqa: E731
        rows.append([
            s, eq_name.get(s) or names.get(s) or tg("tv_desc") or s, tg("sector"), tg("industry"), nse_ind.get(s),
            sec_idx.get(s), ",".join(idx_tags.get(s, [])) or None, int(s in fo_set), int(bool(sme[i])),
            *[m.get(k) for k in stock_cols[9:]],
        ])
    rows.sort(key=lambda r: -(r[stock_cols.index("mcap")] or 0))

    # series file: widely followed stocks (any index list) + F&O
    series_set = sorted({s for code in members for s in members[code]} | fo_set)
    series_set = [s for s in series_set if s in col_of]
    s0 = max(0, T - SERIES_SESSIONS)
    ser = {
        "start": int(s0),
        "syms": series_set,
        "c": [[sig(x) for x in Cf[s0:, col_of[s]]] for s in series_set],
        "weeks": [str(dates[w].date()) for w in week_rows[-WEEKS_RS_HISTORY:]],
        "rsr": [rs_hist.get(s, []) for s in series_set],
    }

    meta = {
        "generated": gen,
        "nse_date": str(nse_date.date()),
        "latest": str(latest.date()),
        "live": live,
        "live_indices": stale_note,
        "history_from": str(dates[0].date()),
        "stocks": len(rows),
        "indices": len(idx_codes),
        "core": [c for c in ni.CORE_SECTORS if c in idx_codes],
        "rrg_n_weekly": RRG_N_WEEKLY,
        "sources": {
            "stocks": "NSE official EOD bhavcopy (nsearchives.nseindia.com), split/bonus adjusted",
            "indices": "NSE ind_close_all daily index file (nsearchives.nseindia.com)",
            "constituents": "NSE index constituent CSVs",
            "classification": "TradingView screener feed (sector / industry)",
            "live": "TradingView (approx. 15-min delayed); other indices estimated from constituents" if live else None,
        },
    }
    core = {
        "meta": meta,
        "dates": [str(d.date()) for d in dates],
        "idx": {
            "codes": idx_codes,
            "meta": idx_meta,
            "c": [[sig(x, 6) for x in IC[:, j]] for j in range(len(idx_codes))],
        },
        "members": {k: v for k, v in members.items() if k in idx_codes or k in dict(BROAD_UNIVERSES)},
        "member_src": member_src,
        "stocks": {"cols": stock_cols, "rows": rows},
    }
    b0 = max(0, T - BREADTH_SESSIONS)
    bre = {"start": int(b0), "keys": breadth["keys"], "universes": [
        {"code": c, "label": lbl, "n": len(cols), "data": {k: v[b0:] for k, v in breadth["data"][c].items()}}
        for c, lbl, cols in universes if c in breadth["data"]]}

    def dump(name, var, obj):
        p = site / name
        js = f"window.{var}=" + json.dumps(obj, separators=(",", ":"), allow_nan=False) + ";\n"
        p.write_text(js, encoding="utf-8")
        log(f"wrote {p.name} ({len(js) / 1e6:.2f} MB)")

    dump("rotation-data.js", "ROT_DATA", clean(core))
    dump("rotation-series.js", "ROT_SERIES", clean(ser))
    dump("breadth-data.js", "BREADTH", clean(bre))

    # roll helpers: state after the last NSE session (a provisional live row, if any, is left out), so the page
    # can apply TradingView quotes itself every minute in market hours
    te = int(dates.searchsorted(nse_date))
    if te < T and dates[te] == nse_date:
        fl = {}
        if tv is not None and "float_pct" in tv.columns:
            fl = {k: v for k, v in zip(tv["tv"], pd.to_numeric(tv["float_pct"], errors="coerce")) if pd.notna(v)}
        float_w = np.array([fl.get(fnd.tv_key(s), 50.0) for s in syms], dtype=float) / 100.0
        sl = slice(0, te + 1)
        lh = live_helpers(syms, dates[sl], C[sl], Cf[sl], Hf[sl], Lf[sl], V[sl], VAL[sl], ISS[sl], DEL[sl], traded[sl],
                          nbars[sl], sme, None if b50 is None else b50[sl], None if b500 is None else b500[sl], IC[sl],
                          idx_codes, sec_idx, float_w, fo_set)
        lh["meta"]["generated"] = gen
        dump("rotation-live.js", "ROT_LIVE", clean(lh))
    return 0


# --------------------------------------------------------------------------- live-roll helpers (rotation-live.js)
def _g10(x):
    """Exact float (the page recomputes the build's rounded outputs, so helper values must not be rounded)."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _trail_nan(a: np.ndarray) -> np.ndarray:
    """Per column: number of NaN values at the end (0 if the last value is finite)."""
    fin = np.isfinite(a)
    out = np.zeros(a.shape[1], dtype=int)
    alive = np.ones(a.shape[1], dtype=bool)
    for t in range(a.shape[0] - 1, -1, -1):
        alive &= ~fin[t]
        if not alive.any():
            break
        out += alive
    return out


def _prior(a: np.ndarray, n: int, fn: str):
    """Window of the n-1 rows before the next session (rows T-n+1 .. T-1)."""
    blk = a[max(0, a.shape[0] - (n - 1)):]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        if fn == "sum_all":  # sum only if all n-1 values exist (rolling(n, min_periods=n))
            ok = np.isfinite(blk).all(axis=0) & (blk.shape[0] == n - 1)
            return np.where(ok, np.nansum(blk, axis=0), np.nan)
        if fn == "max":
            return np.nanmax(np.where(np.isfinite(blk), blk, -np.inf), axis=0)
        if fn == "min":
            return np.nanmin(np.where(np.isfinite(blk), blk, np.inf), axis=0)
        if fn == "count":
            return np.isfinite(blk).sum(axis=0)
    raise ValueError(fn)


def live_helpers(syms, dates, C, Cf, Hf, Lf, V, VAL, ISS, DEL, traded, nbars, sme, b50, b500, IC, idx_codes,
                 sec_idx, float_w, fo_set):
    """State after the last NSE session, so the page can roll every metric forward by one live session
    (TradingView quotes) exactly as this script would. All arrays must end at the last NSE EOD session."""
    T, N = Cf.shape
    e = T - 1
    nxt = bd.next_weekday(dates[-1])
    wk_of = lambda d: int(np.datetime64(d, "D").astype(np.int64)) - (int(np.datetime64(d, "D").astype(np.int64)) + 3) % 7  # noqa: E731
    new_week = wk_of(nxt) != wk_of(dates[-1])
    H = {}

    def put(k, arr, f=_g10):
        H[k] = [f(x) for x in arr]

    put("c", Cf[e]); put("v", np.where(traded[e], V[e], np.nan)); put("iss", ISS[e]); put("ff", float_w)
    gap = np.zeros(N, dtype=int)
    run = np.ones(N, dtype=bool)
    for t in range(e, max(-1, e - 8), -1):
        run &= ~traded[t]
        gap += run
    H["gap"] = gap.tolist()
    for k in (5, 21, 63, 126, 189, 252):
        put(f"a{k}", Cf[e + 1 - k] if e + 1 - k >= 0 else np.full(N, np.nan))
    H["nb"] = nbars[e].astype(int).tolist()

    # RS Rating at the rows 5 / 20 / 63 sessions before the next one
    legs = [(63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)]

    def rating_row(t):
        if t < 0:
            return np.full(N, np.nan)
        num = np.zeros(N)
        den = np.zeros(N)
        for k, w in legs:
            with np.errstate(divide="ignore", invalid="ignore"):
                leg = Cf[t] / Cf[t - k] if t - k >= 0 else np.full(N, np.nan)
            ok = np.isfinite(leg) & (nbars[t] > k)
            num += np.where(ok, w * leg, 0)
            den += np.where(ok, w, 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            s_t = np.where(den >= 0.4 - 1e-9, num / den, np.nan)
        ref = np.sort(s_t[~sme & np.isfinite(s_t)])
        out = np.full(N, np.nan)
        if len(ref) >= 50:
            fin = np.isfinite(s_t)
            out[fin] = np.clip(np.floor(np.searchsorted(ref, s_t[fin], side="right") / len(ref) * 98.999) + 1, 1, 99)
        return out

    for k, key in ((5, "rs5"), (20, "rs20"), (63, "rs63")):
        put(key, rating_row(e + 1 - k), lambda x: None if not np.isfinite(x) else int(x))

    # weekly: Mansfield (52-week SMA of the RS line), RRG (n = RRG_N_WEEKLY) and Weinstein stage
    wk = week_last_rows(dates)
    hist = wk if new_week else wk[:-1]
    Cw = Cf[hist]
    sec_cols = np.array([idx_codes.index(sec_idx[s]) if sec_idx.get(s) in idx_codes else -1 for s in syms])
    SB = np.full((len(hist), N), np.nan)
    for j in set(sec_cols.tolist()):
        if j >= 0:
            SB[:, sec_cols == j] = IC[hist, j].reshape(-1, 1)
    n = RRG_N_WEEKLY
    wts = np.arange(n - 1, 0, -1, dtype=float)  # weights of weeks W-1 .. W-9

    def kpart(a):  # sum_{i=1..n-1} (n-i) * a[W-i]   (NaN unless all present)
        if a.shape[0] < n - 1:
            return np.full(N, np.nan)
        blk = a[-(n - 1):][::-1]
        return (blk * wts.reshape(-1, 1)).sum(axis=0)

    for key, bw in (("50", None if b50 is None else b50[hist].reshape(-1, 1)), ("500", None if b500 is None else b500[hist].reshape(-1, 1)), ("sec", SB)):
        if bw is None:
            put("m" + key, np.full(N, np.nan))
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            rsd = Cw / bw
        blk = rsd[-51:]
        ok = np.isfinite(blk).all(axis=0) & (blk.shape[0] == 51)
        put("m" + key, np.where(ok, blk.sum(axis=0), np.nan))
        if key == "50":
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            w_ = wma(rsd, n)
            base = rsd / w_
            ratio = 100 * wma(base, n)
            mom = 100 * ratio / wma(ratio, n)
        tag = "500" if key == "500" else "s"
        put(f"k{tag}a", kpart(base)); put(f"k{tag}b", kpart(rsd)); put(f"k{tag}c", kpart(ratio))
        H[f"q{tag}p"] = [quadrant(ratio[-1, i], mom[-1, i]) if len(hist) else None for i in range(N)]
    ma30 = sma(Cw, 30)
    blk = Cw[-29:]
    ok = np.isfinite(blk).all(axis=0) & (blk.shape[0] == 29)
    put("s29", np.where(ok, blk.sum(axis=0), np.nan))
    nwk = len(hist) + 1
    put("m4", ma30[-4] if len(hist) >= 4 else np.full(N, np.nan))
    put("m20", (ma30[-20] if nwk > 21 else ma30[0]) if len(hist) else np.full(N, np.nan))
    H_meta = {"new_week": bool(new_week), "nwk": nwk}

    # daily windows ending at the next session
    for nn in (20, 50, 100, 150, 200):
        put(f"s{nn - 1}", _prior(Cf, nn, "sum_all"))
    s200 = sma(Cf, 200)
    put("s200m", s200[e - 20] if e - 20 >= 0 else np.full(N, np.nan))
    for nn in (21, 63, 252):
        put(f"h{nn - 1}", _prior(Hf, nn, "max")); put(f"l{nn - 1}", _prior(Lf, nn, "min"))
        H[f"n{nn - 1}"] = _prior(Hf, nn, "count").astype(int).tolist()
    H = {k: [None if (isinstance(x, float) and not math.isfinite(x)) else x for x in v] for k, v in H.items()}
    if b500 is not None:
        with np.errstate(divide="ignore", invalid="ignore"):
            rl = Cf / b500.reshape(-1, 1)
        put("rlmax", _prior(rl, 252, "max"))
        H["rln"] = _prior(rl, 252, "count").astype(int).tolist()
        hi = pd.DataFrame(rl).rolling(252, min_periods=126).max().to_numpy()
        at_hi = rl >= hi * 0.9999
        H["nh4"] = [int(x) for x in at_hi[-4:].any(axis=0)]
    chg = Cf - shift(Cf, 1)
    v49, c49 = V[-49:], chg[-49:]
    with np.errstate(invalid="ignore"):
        put("up49", np.nansum(np.where(c49 > 0, v49, 0), axis=0)); put("dn49", np.nansum(np.where(c49 < 0, v49, 0), axis=0))
    put("val19", np.nansum(VAL[-19:], axis=0)); H["nval19"] = np.isfinite(VAL[-19:]).sum(axis=0).astype(int).tolist()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        put("dl9", np.nanmean(DEL[-9:], axis=0))
    H["al9"] = [int(x) for x in traded[-9:].any(axis=0)]
    H["sme"] = [int(bool(x)) for x in sme]
    H["fo"] = [int(s in fo_set) for s in syms]

    # indicator states for the breadth diffusion (ewm(adjust=False) carries a weight across NaN gaps)
    d = np.diff(Cf, axis=0, prepend=np.nan)
    upi = np.where(np.isnan(d), np.nan, np.clip(d, 0, None))
    dni = np.where(np.isnan(d), np.nan, np.clip(-d, 0, None))
    put("ru", ewm(upi, alpha=1 / 14)[e]); put("rd", ewm(dni, alpha=1 / 14)[e]); H["gr"] = _trail_nan(d).tolist()
    e12, e26 = ewm(Cf, span=12), ewm(Cf, span=26)
    line = e12 - e26
    put("e12", e12[e]); put("e26", e26[e]); put("sg", ewm(line, span=9)[e])
    H["gc"] = _trail_nan(Cf).tolist(); H["gs"] = _trail_nan(line).tolist()
    pc, ph, pl = shift(Cf, 1), shift(Hf, 1), shift(Lf, 1)
    tr = np.fmax(Hf - Lf, np.fmax(np.abs(Hf - pc), np.abs(Lf - pc)))
    upm, dnm = Hf - ph, pl - Lf
    pdm = np.where((upm > dnm) & (upm > 0), upm, 0.0)
    ndm = np.where((dnm > upm) & (dnm > 0), dnm, 0.0)
    pdm[np.isnan(tr)] = np.nan
    ndm[np.isnan(tr)] = np.nan
    atr = ewm(tr, alpha=1 / 14)
    ep, en = ewm(pdm, alpha=1 / 14), ewm(ndm, alpha=1 / 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi, ndi = 100 * ep / atr, 100 * en / atr
        dx = 100 * np.abs(pdi - ndi) / (pdi + ndi)
    put("atr", atr[e]); put("ep", ep[e]); put("en", en[e]); put("adx", ewm(dx, alpha=1 / 14)[e])
    H["gt"] = _trail_nan(tr).tolist(); H["gx"] = _trail_nan(dx).tolist()
    put("hp", Hf[e]); put("lp", Lf[e])
    d5 = Cf - shift(Cf, 5)
    put("mu", ewm(np.where(np.isnan(d5), np.nan, np.clip(d5, 0, None)), alpha=1 / 20)[e])
    put("md", ewm(np.where(np.isnan(d5), np.nan, np.clip(-d5, 0, None)), alpha=1 / 20)[e])
    H["gm"] = _trail_nan(d5).tolist()
    H = {k: [None if (isinstance(x, float) and not math.isfinite(x)) else x for x in v] for k, v in H.items()}
    meta = {"eod": str(pd.Timestamp(dates[-1]).date()), "next": str(nxt.date()), **H_meta,
            "tv_index": ni.TV_INDEX, "rrg_n": RRG_N_WEEKLY,
            # unrounded index closes of the last session (the data file keeps 6 significant digits)
            "idx_prev": {c: _g10(IC[e, j]) for j, c in enumerate(idx_codes)}}
    return {"meta": meta, "syms": list(syms), "h": H}


def clean(o):
    """Replace NaN/inf with None recursively (json allow_nan=False)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.floating,)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return o


# --------------------------------------------------------------------------- reference helpers
def load_or_cache_tv_classes(meta: Path, tv: pd.DataFrame | None) -> pd.DataFrame | None:
    """Keep a copy of TradingView's sector/industry/type/float per stock so a failed request still classifies."""
    p = Path(meta) / "tv_classification.csv"
    keep_cols = ["tv", "tv_desc", "tv_type", "sector", "industry", "float_pct"]
    if tv is not None and not tv.empty:
        tv[[c for c in keep_cols if c in tv.columns]].sort_values("tv").to_csv(p, index=False)
        return tv
    if p.exists():
        log("TradingView unavailable - using cached sector/industry classification")
        return pd.read_csv(p, dtype={"tv": str}, keep_default_na=False, na_values={"float_pct": [""]})
    return None


def build_members(doc: dict, ref: dict, syms: list[str]) -> tuple[dict[str, list[str]], dict[str, str]]:
    members = {k: list(v) for k, v in (doc.get("members") or {}).items()}
    src = {k: "nse" for k in members}
    broad, industry = ref.get("index", ({}, {}))
    for old, code in OLD_BROAD_KEYS.items():  # screener's cached broad lists as fallback
        if code not in members and old in broad:
            members[code] = sorted(broad[old])
            src[code] = "nse"
    ind_all = {**industry, **(doc.get("industry") or {})}
    n500 = set(members.get("NIFTY500", []))
    for code, ind in ni.INDUSTRY_FALLBACK.items():
        if code not in members:
            approx = sorted(s for s in n500 if ind_all.get(s) == ind)
            if len(approx) >= 5:
                members[code] = approx
                src[code] = "approx"
    present = set(syms)
    return {k: [s for s in v if s in present] for k, v in members.items()}, src


def assign_sector_index(syms, members, nse_industry, ref, tv, have_idx: set) -> dict[str, str]:
    ind_all = {**(ref.get("index", ({}, {}))[1] or {}), **(nse_industry or {})}
    bank = set(members.get("NIFTYBANK", [])) | set(members.get("NIFTYPSUBANK", [])) | set(members.get("NIFTYPRIVATEBANK", []))
    pharma = set(members.get("NIFTYPHARMA", []))
    capmkt = set(members.get("NIFTYCAPITALMARKETS", []))
    tvd = tv.set_index("tv") if tv is not None else pd.DataFrame()

    def first(cands):
        return next((c for c in cands if c in have_idx), None)

    out = {}
    for s in syms:
        code = None
        if s in bank:
            code = first(["NIFTYBANK"])
        elif s in capmkt:
            code = first(["NIFTYCAPITALMARKETS"])
        elif s in pharma:
            code = first(["NIFTYPHARMA"])
        if code is None and ind_all.get(s) in NSE_INDUSTRY_TO_INDEX:
            code = first(NSE_INDUSTRY_TO_INDEX[ind_all[s]])
        if code is None and fnd.tv_key(s) in tvd.index:
            r = tvd.loc[fnd.tv_key(s)]
            code = first(TV_INDUSTRY_TO_INDEX.get(str(r.get("industry")), [])) or \
                first(TV_SECTOR_TO_INDEX.get(str(r.get("sector")), []))
        out[s] = code or first(["NIFTY500", "NIFTY50"])
    return out


def build_index_matrix(ix: pd.DataFrame, dates: pd.DatetimeIndex, members, Cf, C, ISS, col_of, tv, session):
    """Closes of every equity index on the stock-session calendar.
    Sessions newer than NSE's last index file (or today's live session) are filled with TradingView quotes
    where available, otherwise estimated from constituents' free-float-weighted returns."""
    if ix.empty:
        log("WARNING: no index files stored yet - the page will show stock metrics only")
    last_name = ix.sort_values("date").groupby("code")["name"].last() if not ix.empty else pd.Series(dtype=str)
    piv = (ix.pivot_table(index="date", columns="code", values="close", aggfunc="last")
           if not ix.empty else pd.DataFrame(index=dates))
    piv = piv.reindex(dates)
    last_real = {c: piv[c].last_valid_index() for c in piv.columns}
    # drop indices that stopped being published long ago or have very little history
    codes = [c for c in piv.columns
             if last_real[c] is not None and last_real[c] >= dates[-1] - pd.Timedelta(days=20)
             and piv[c].notna().sum() >= 20]
    codes.sort(key=lambda c: (["broad", "sector", "thematic", "strategy", "vix"].index(ni.index_category(last_name[c]) or "vix"),
                              ni.index_label(last_name[c])))
    IC = piv[codes].to_numpy(dtype=float, copy=True) if codes else np.zeros((len(dates), 0))
    tvq = fetch_tv_indices(session) if session is not None else {}
    fl = {}
    if tv is not None and "float_pct" in tv.columns:
        fl = {k: v for k, v in zip(tv["tv"], pd.to_numeric(tv["float_pct"], errors="coerce")) if pd.notna(v)}
    float_w = np.array([fl.get(fnd.tv_key(s), 50.0) for s in col_of], dtype=float) / 100.0
    info = {"estimated": [], "tv": [], "stale": [], "dates": []}
    T = len(dates)
    for j, code in enumerate(codes):
        lr = last_real[code]
        first_missing = dates.searchsorted(lr, side="right")
        if first_missing >= T:
            continue
        for t in range(first_missing, T):
            prev = IC[t - 1, j]
            val = np.nan
            if t == T - 1 and code in tvq and tvq[code].get("close"):
                val, how = float(tvq[code]["close"]), "tv"
            else:
                cols = [col_of[s] for s in members.get(code, []) if s in col_of]
                if len(cols) >= 5:
                    r = C[t, cols] / Cf[t - 1, cols] - 1
                    w = Cf[t - 1, cols] * ISS[t - 1, cols] * float_w[cols]
                    ok = np.isfinite(r) & np.isfinite(w) & (np.abs(r) < 0.25)
                    if ok.sum() >= max(3, 0.6 * len(cols)):
                        val, how = prev * (1 + np.sum(r[ok] * w[ok]) / np.sum(w[ok])), "estimated"
            if not np.isfinite(val):
                val, how = prev, "stale"
            IC[t, j] = val
            info[how].append(last_name[code])
            info["dates"].append(str(dates[t].date()))
    # forward-fill internal gaps (an index file missing for a day)
    IC = pd.DataFrame(IC).ffill().to_numpy(copy=True)
    info = {k: sorted(set(v)) for k, v in info.items()}
    if info["estimated"] or info["tv"] or info["stale"]:
        log(f"provisional index values for {info['dates']}: {len(info['tv'])} TradingView, "
            f"{len(info['estimated'])} estimated, {len(info['stale'])} carried forward")
    meta = []
    for c in codes:
        name = last_name[c]
        meta.append({"code": c, "name": name, "label": ni.index_label(name), "cat": ni.index_category(name),
                     "members": len(members.get(c, [])) or None, "first": str(piv[c].first_valid_index().date())})
    # latest valuation (P/E, P/B, yield) from NSE's file
    if not ix.empty:
        lastrow = ix.sort_values("date").groupby("code").last()
        for m in meta:
            if m["code"] in lastrow.index:
                r = lastrow.loc[m["code"]]
                m.update({"pe": rnd(r.get("pe")), "pb": rnd(r.get("pb")), "dy": rnd(r.get("dy"))})
    return codes, meta, IC, info


# --------------------------------------------------------------------------- stock metrics
def stock_metrics(syms, dates, C, Cf, Hf, Lf, V, VAL, ISS, DEL, traded, nbars, sme, b50, b500, IC, idx_codes, sec_idx):
    T, N = Cf.shape
    last = T - 1

    def ret(k):
        if T <= k:
            return np.full(N, np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            return (Cf[last] / Cf[last - k] - 1) * 100

    # ---- RS Rating (IBD style) for every session
    legs = [(63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)]
    num = np.zeros_like(Cf)
    den = np.zeros_like(Cf)
    for k, w in legs:
        with np.errstate(divide="ignore", invalid="ignore"):
            leg = Cf / shift(Cf, k)
        ok = np.isfinite(leg) & (nbars > k)
        num += np.where(ok, w * leg, 0)
        den += np.where(ok, w, 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        score = np.where(den >= 0.4 - 1e-9, num / den, np.nan)
    partial = den[last] < 0.999
    main = ~sme
    rating = np.full_like(score, np.nan)
    for t in range(T):
        s_t = score[t]
        ref = s_t[main & np.isfinite(s_t)]
        if len(ref) < 50:
            continue
        ref = np.sort(ref)
        fin = np.isfinite(s_t)
        pct = np.searchsorted(ref, s_t[fin], side="right") / len(ref)
        rating[t, fin] = np.clip(np.floor(pct * 98.999) + 1, 1, 99)

    def rating_ago(k):
        return rating[last - k] if T > k else np.full(N, np.nan)

    # ---- RS line vs benchmarks
    def rs_line(b):
        if b is None:
            return None
        with np.errstate(divide="ignore", invalid="ignore"):
            return Cf / b.reshape(-1, 1)

    rl500 = rs_line(b500)
    rsnh = rsnh_bp = rsnh5 = np.full(N, np.nan)
    if rl500 is not None:
        hi = pd.DataFrame(rl500).rolling(252, min_periods=126).max().to_numpy()
        at_hi = rl500 >= hi * 0.9999
        price_hi = pd.DataFrame(Hf).rolling(252, min_periods=126).max().to_numpy()
        rsnh = at_hi[last].astype(float)
        rsnh5 = at_hi[-5:].any(axis=0).astype(float)
        # O'Neil "blue dot": RS line at a new 52-week high (within the last 5 sessions) while price is
        # still at least 3% below its own 52-week high, i.e. the stock leads the market before breaking out
        rsnh_bp = (at_hi[-5:].any(axis=0) & (Cf[last] < price_hi[last] * 0.97)).astype(float)
        nohist = np.isnan(hi[last])
        for a in (rsnh, rsnh5, rsnh_bp):
            a[nohist] = np.nan

    # ---- weekly series: Mansfield RS, RRG quadrants, Weinstein stage
    wk = week_last_rows(dates)
    Cw = Cf[wk]

    def mansfield(bw):
        with np.errstate(divide="ignore", invalid="ignore"):
            rsd = Cw / bw.reshape(-1, 1) if bw.ndim == 1 else Cw / bw
            return (rsd / sma(rsd, 52) - 1) * 100

    mrs50 = mansfield(b50[wk])[-1] if b50 is not None else np.full(N, np.nan)
    mrs500 = mansfield(b500[wk])[-1] if b500 is not None else np.full(N, np.nan)
    sec_cols = np.array([idx_codes.index(sec_idx[s]) if sec_idx.get(s) in idx_codes else -1 for s in syms])
    SB = np.full((len(wk), N), np.nan)
    for j in set(sec_cols.tolist()):
        if j >= 0:
            SB[:, sec_cols == j] = IC[wk, j].reshape(-1, 1)
    mrs_sec = mansfield(SB)[-1]
    q500 = q500p = qsec = qsecp = [None] * N
    if b500 is not None:
        r5, m5 = rrg(Cw, b500[wk], RRG_N_WEEKLY)
        q500 = [quadrant(r5[-1, i], m5[-1, i]) for i in range(N)]
        q500p = [quadrant(r5[-2, i], m5[-2, i]) if len(wk) > 1 else None for i in range(N)]
    rs_, ms_ = rrg(Cw, SB, RRG_N_WEEKLY)
    qsec = [quadrant(rs_[-1, i], ms_[-1, i]) for i in range(N)]
    qsecp = [quadrant(rs_[-2, i], ms_[-2, i]) if len(wk) > 1 else None for i in range(N)]

    ma30 = sma(Cw, 30)
    stage = np.full(N, np.nan)
    if len(wk) >= 35:
        m_now, m_4, m_20 = ma30[-1], ma30[-5], ma30[-21] if len(wk) > 21 else ma30[0]
        with np.errstate(divide="ignore", invalid="ignore"):
            slope = m_now / m_4 - 1
            prior = m_now / m_20 - 1
        px = Cw[-1]
        for i in range(N):
            if not (np.isfinite(slope[i]) and np.isfinite(px[i])):
                continue
            if slope[i] > 0.01:
                stage[i] = 2 if px[i] >= m_now[i] else 3
            elif slope[i] < -0.01:
                stage[i] = 4 if px[i] < m_now[i] else 1
            else:
                stage[i] = 3 if (np.isfinite(prior[i]) and prior[i] > 0.03) else 1

    # ---- Minervini trend template
    s50, s150, s200 = sma(Cf, 50)[last], sma(Cf, 150)[last], sma(Cf, 200)
    s200_now, s200_1m = s200[last], s200[last - 21] if T > 21 else np.full(N, np.nan)
    hi52 = pd.DataFrame(Hf).rolling(252, min_periods=200).max().to_numpy()[last]
    lo52 = pd.DataFrame(Lf).rolling(252, min_periods=200).min().to_numpy()[last]
    px = Cf[last]
    with np.errstate(invalid="ignore"):
        rules = np.vstack([
            (px > s150) & (px > s200_now), s150 > s200_now, s200_now > s200_1m,
            (s50 > s150) & (s50 > s200_now), px > s50, px >= 1.3 * lo52, px >= 0.75 * hi52,
            rating[last] >= 70,
        ])
    tt = rules.sum(axis=0).astype(float)
    tt[np.isnan(s200_now) | np.isnan(hi52)] = np.nan

    # 52-week high/low distance (use what history exists if < 1 year)
    hi_any = pd.DataFrame(Hf).rolling(252, min_periods=20).max().to_numpy()[last]
    lo_any = pd.DataFrame(Lf).rolling(252, min_periods=20).min().to_numpy()[last]

    # up/down volume ratio (50 sessions), avg turnover, delivery
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    chg = Cf - shift(Cf, 1)
    v50 = V[-50:]
    c50 = chg[-50:]
    with np.errstate(divide="ignore", invalid="ignore"):
        udv = np.nansum(np.where(c50 > 0, v50, 0), axis=0) / np.nansum(np.where(c50 < 0, v50, 0), axis=0)
        val_cr = np.nanmean(VAL[-20:], axis=0) / 1e7
        deliv = np.nanmean(DEL[-10:], axis=0)
        chg1 = (Cf[last] / Cf[last - 1] - 1) * 100
    mcap = Cf[last] * ISS[last] / 1e7
    r1w, r1m, r3m, r6m, r1y = ret(5), ret(21), ret(63), ret(126), ret(252)

    out = []
    for i in range(N):
        if not np.isfinite(Cf[last, i]):
            out.append(None)
            continue
        out.append({
            "close": sig(Cf[last, i], 6), "chg": rnd(chg1[i]),
            "r1w": rnd(r1w[i], 1), "r1m": rnd(r1m[i], 1), "r3m": rnd(r3m[i], 1), "r6m": rnd(r6m[i], 1), "r1y": rnd(r1y[i], 1),
            "rs": rnd(rating[last, i], 0), "rs1w": rnd(rating_ago(5)[i], 0), "rs4w": rnd(rating_ago(20)[i], 0),
            "rs13w": rnd(rating_ago(63)[i], 0), "rs_part": int(bool(partial[i])) if np.isfinite(rating[last, i]) else None,
            "mrs50": rnd(mrs50[i], 1), "mrs500": rnd(mrs500[i], 1), "mrs_sec": rnd(mrs_sec[i], 1),
            "rsnh": rnd(rsnh[i], 0), "rsnh_bp": rnd(rsnh_bp[i], 0), "rsnh5": rnd(rsnh5[i], 0),
            "q500": q500[i], "q500p": q500p[i], "qsec": qsec[i], "qsecp": qsecp[i],
            "stage": rnd(stage[i], 0), "tt": rnd(tt[i], 0),
            "off_hi": rnd((Cf[last, i] / hi_any[i] - 1) * 100, 1) if np.isfinite(hi_any[i]) and hi_any[i] > 0 else None,
            "above_lo": rnd((Cf[last, i] / lo_any[i] - 1) * 100, 1) if np.isfinite(lo_any[i]) and lo_any[i] > 0 else None,
            "udv": rnd(udv[i], 2), "val_cr": rnd(val_cr[i], 2), "mcap": rnd(mcap[i], 0), "deliv": rnd(deliv[i], 1),
            "bars": int(nbars[last, i]),
        })
    # weekly RS-rating history (52 weeks) for the chart series
    hist = {}
    wr = wk[-WEEKS_RS_HISTORY:]
    for i, s in enumerate(syms):
        hist[s] = [rnd(x, 0) for x in rating[wr, i]]
    return out, hist, wk


# --------------------------------------------------------------------------- breadth
BREADTH_KEYS = [
    # key, description
    ("a20", "% above 20-day SMA"), ("a50", "% above 50-day SMA"), ("a100", "% above 100-day SMA"),
    ("a200", "% above 200-day SMA"), ("rsi", "% with RSI(14) >= 50"), ("macd", "% with MACD above signal"),
    ("adx", "% with ADX(14) >= 25"), ("rmi", "% with RMI(20,5) > 50"),
    ("h1m", "% at 1-month high"), ("h3m", "% at 3-month high"), ("h52", "% at 52-week high"),
    ("l1m", "% at 1-month low"), ("l3m", "% at 3-month low"), ("l52", "% at 52-week low"),
    ("adv", "Advancing stocks"), ("dec", "Declining stocks"), ("unch", "Unchanged stocks"),
    ("up4", "Stocks up 4% or more"), ("dn4", "Stocks down 4% or more"),
]


def breadth_series(dates, C, Cf, Hf, Lf, traded, alive, nbars, universes):
    T, N = Cf.shape
    conds = {}

    def put(key, cond, valid):
        conds[key] = (np.where(valid, cond, False), valid)

    with np.errstate(invalid="ignore"):
        for n in (20, 50, 100, 200):
            m = sma(Cf, n)
            put(f"a{n}", Cf > m, alive & np.isfinite(m))
        r = rsi(Cf)
        put("rsi", r >= 50, alive & (nbars > 15) & np.isfinite(r))
        put("macd", macd_buy(Cf), alive & (nbars > 35))
        a = adx(Hf, Lf, Cf)
        put("adx", a >= 25, alive & (nbars > 28) & np.isfinite(a))
        rm = rmi(Cf)
        put("rmi", rm > 50, alive & (nbars > 25) & np.isfinite(rm))
        for key, n in (("1m", 21), ("3m", 63), ("52", 252)):
            hh = pd.DataFrame(Hf).rolling(n, min_periods=int(n * 0.8)).max().to_numpy()
            ll = pd.DataFrame(Lf).rolling(n, min_periods=int(n * 0.8)).min().to_numpy()
            ok = traded & np.isfinite(hh) & (nbars >= int(n * 0.8))
            put(f"h{key}", Hf >= hh, ok)
            put(f"l{key}", Lf <= ll, ok)
        prev = shift(Cf, 1)
        chg = C / prev - 1
        okc = traded & np.isfinite(prev)
    out = {}
    pct_keys = [k for k, _ in BREADTH_KEYS if k not in ("adv", "dec", "unch", "up4", "dn4")]
    for code, _lbl, cols in universes:
        if len(cols) < 5:
            continue
        cols = np.asarray(cols)
        d = {}
        for k in pct_keys:
            cond, valid = conds[k]
            n_ok = valid[:, cols].sum(axis=1)
            n_c = cond[:, cols].sum(axis=1)
            with np.errstate(divide="ignore", invalid="ignore"):
                p = np.where(n_ok >= max(3, 0.2 * len(cols)), np.round(1000 * n_c / n_ok), np.nan)
            d[k] = [None if not np.isfinite(x) else int(x) for x in p]  # % x 10
        ch = chg[:, cols]
        ok = okc[:, cols]
        with np.errstate(invalid="ignore"):
            d["adv"] = (ok & (ch > 0)).sum(axis=1).astype(int).tolist()
            d["dec"] = (ok & (ch < 0)).sum(axis=1).astype(int).tolist()
            d["unch"] = (ok & (ch == 0)).sum(axis=1).astype(int).tolist()
            d["up4"] = (ok & (ch >= 0.04)).sum(axis=1).astype(int).tolist()
            d["dn4"] = (ok & (ch <= -0.04)).sum(axis=1).astype(int).tolist()
        out[code] = d
    log(f"breadth: {len(out)} universes x {len(BREADTH_KEYS)} indicators")
    return {"keys": [{"k": k, "d": desc} for k, desc in BREADTH_KEYS], "data": out}


if __name__ == "__main__":
    sys.exit(main())
