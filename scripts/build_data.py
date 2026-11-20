"""
Build site/data.js for the NSE Camarilla Screener.

    python scripts/build_data.py            # download anything new from NSE, then build
    python scripts/build_data.py --no-download   # rebuild from stored data only

Pipeline
  1. Download missing NSE EOD days (first run back-fills from 1 Jan of last year).
  2. Join history across symbol renames, adjust for splits / bonuses (TradingView-style).
  3. Optionally add today's provisional bar from TradingView during market hours.
  4. Per stock: previous / current Daily, Weekly, Monthly, Quarterly, Yearly H-L-C
     (the browser turns these into Camarilla or other pivot levels), RSI, EMAs, returns...
  5. Merge fundamentals and write one compact JavaScript file the web page loads.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fundamentals as fnd  # noqa: E402
import nse_data as nse  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")
TFS = ["D", "W", "M", "Q", "Y"]
SME_SERIES = {"SM", "ST"}


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


# --------------------------------------------------------------------------- helpers
def num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def rnd(x, nd=2):
    f = num(x)
    return None if f is None else round(f, nd)


def ratio(a, b):
    a, b = num(a), num(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


# --------------------------------------------------------------------------- reference data (cached in data/meta)
def load_reference(session, meta: Path, offline: bool):
    meta.mkdir(parents=True, exist_ok=True)
    ref = {}

    def cached(name, fetch, save, load):
        p = meta / name
        if not offline:
            try:
                val = fetch()
                if val is not None and (not hasattr(val, "__len__") or len(val) > 0):
                    save(val, p)
                    return val
            except Exception as exc:
                log(f"{name}: fetch failed ({exc}); using cached copy")
        if p.exists():
            return load(p)
        return None

    ref["equity"] = cached(
        "equity_list.csv", lambda: nse.fetch_equity_list(session),
        lambda v, p: v.sort_values("symbol").to_csv(p, index=False),
        lambda p: pd.read_csv(p, dtype=str, keep_default_na=False))
    ref["symchg"] = cached(
        "symbol_changes.csv", lambda: nse.fetch_symbol_changes(session),
        lambda v, p: v.sort_values(["date", "old"]).to_csv(p, index=False, date_format="%Y-%m-%d"),
        lambda p: pd.read_csv(p, dtype=str, keep_default_na=False).assign(date=lambda d: pd.to_datetime(d["date"])))

    def save_idx(v, p):
        members, industry = v
        p.write_text(json.dumps({"members": {k: sorted(s) for k, s in sorted(members.items())},
                                 "industry": dict(sorted(industry.items()))}, indent=0))

    def load_idx(p):
        j = json.loads(p.read_text())
        return {k: set(v) for k, v in j["members"].items()}, j.get("industry", {})

    def fetch_idx():
        r = nse.fetch_index_members(session)
        return r if r[0] else None  # all index files missing -> keep cached copy

    idx = cached("index_members.json", fetch_idx, save_idx, load_idx)
    ref["index"] = idx or ({}, {})
    fo = cached("fo_symbols.txt", lambda: nse.fetch_fo_symbols(session) or None,
                lambda v, p: p.write_text("\n".join(sorted(v)) + "\n"),
                lambda p: {x.strip() for x in p.read_text().split() if x.strip()})
    ref["fo"] = fo or set()
    names_p = meta / "names.csv"
    ref["names"] = (dict(pd.read_csv(names_p, dtype=str, keep_default_na=False).values)
                    if names_p.exists() else {})
    return ref


# --------------------------------------------------------------------------- history preparation
def apply_symbol_changes(df: pd.DataFrame, changes: pd.DataFrame | None) -> pd.DataFrame:
    if changes is None or len(changes) == 0:
        return df
    present = set(df["symbol"].unique())
    ch = changes[changes["old"].isin(present)].sort_values("date")
    if ch.empty:
        return df
    sym = df["symbol"].to_numpy(dtype=object, copy=True)
    dates = df["date"].to_numpy()
    for old, new, eff in ch[["old", "new", "date"]].itertuples(index=False):
        mask = (sym == old) & (dates < np.datetime64(pd.Timestamp(eff)))
        if mask.any():
            sym[mask] = new
    df = df.copy()
    df["symbol"] = sym
    log(f"applied {len(ch)} symbol change(s)")
    return df


def link_by_isin(df: pd.DataFrame) -> pd.DataFrame:
    """A symbol that stopped trading but whose ISIN now trades under another symbol is a rename."""
    latest = df["date"].max()
    active = df[df["date"] == latest]
    isin_to_sym = {i: s for i, s in zip(active["isin"], active["symbol"]) if i}
    active_syms = set(active["symbol"])
    last = df.sort_values("date").groupby("symbol").tail(1)
    rename = {}
    for s, i in zip(last["symbol"], last["isin"]):
        if s not in active_syms and i and i in isin_to_sym and isin_to_sym[i] != s:
            rename[s] = isin_to_sym[i]
    if rename:
        df = df.copy()
        df["symbol"] = df["symbol"].replace(rename)
        log(f"linked {len(rename)} renamed symbol(s) by ISIN")
    return df


_STD_RATIOS = sorted({b / (a + b) for a in range(1, 11) for b in range(1, 11)}
                     | {1 / n for n in (2, 2.5, 4, 5, 10, 20, 25, 50, 100)}
                     | {float(n) for n in (2, 4, 5, 10, 20, 25, 50, 100)})


def _snap(r: float) -> float | None:
    best = min(_STD_RATIOS, key=lambda c: abs(math.log(r / c)))
    return best if abs(math.log(r / best)) < math.log(1.06) else None


def adjust_corporate_actions(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Back-adjust prices for splits / bonuses / consolidations so period highs & lows match
    TradingView's (split-adjusted) charts.

    Detection, in order of preference:
      A. NSE itself re-based the previous close (PrvsClsgPric != yesterday's close).
      B. NSE issue size (shares, from the daily MCAP file) jumped by k and the price fell ~1/k.
      C. Issue size unknown: price gap beyond any circuit limit that matches a standard ratio.
    """
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    prev_c = g["close"].shift(1)
    prev_issue = g["issue_size"].shift(1)
    pc = df["prev_close"]
    r_nse = pc / prev_c
    k = df["issue_size"] / prev_issue
    rc = df["close"] / pc
    ro = df["open"] / pc

    factor = pd.Series(1.0, index=df.index)
    ev_a = prev_c.gt(0) & pc.gt(0) & (r_nse - 1).abs().gt(0.02)
    factor[ev_a] = r_nse[ev_a]
    ev_b = (~ev_a & k.notna() & ((k >= 1.1) | (k <= 0.91))
            & ((rc * k - 1).abs() < 0.3) & ((ro * k - 1).abs() < 0.3))
    factor[ev_b] = 1.0 / k[ev_b]
    ev_c = (~ev_a & ~ev_b & k.isna() & prev_c.notna() & pc.gt(0)
            & ((ro < 0.75) | (ro > 1.33)) & ((rc < 0.75) | (rc > 1.33)))
    for i in np.flatnonzero(ev_c.to_numpy()):
        s = _snap(float(ro.iat[i]))
        if s is not None:
            factor.iat[i] = s

    df["_f"] = factor.to_numpy()
    rev = df.iloc[::-1].groupby("symbol", sort=False)["_f"].cumprod().iloc[::-1]
    mult = (rev / df["_f"]).to_numpy()  # product of factors strictly after each row
    for c in ("open", "high", "low", "close", "prev_close"):
        df[c] = df[c].to_numpy() * mult
    df["volume"] = df["volume"].to_numpy() / mult
    events = df.loc[df["_f"] != 1.0, ["symbol", "date", "_f"]].rename(columns={"_f": "factor"})
    df = df.drop(columns="_f")
    log(f"corporate-action adjustments: {len(events)} (A={int(ev_a.sum())}, B={int(ev_b.sum())})")
    return df, events


def add_live_bars(df: pd.DataFrame, tv: pd.DataFrame | None, now: dt.datetime):
    """During/after market hours (before NSE's EOD file is out) append today's bar from TradingView."""
    if tv is None or tv.empty:
        return df, None
    latest = df["date"].max()
    today = pd.Timestamp(now.date())
    if today <= latest or now.weekday() >= 5 or now.time() < dt.time(9, 20):
        return df, None
    last = df[df["date"] == latest].drop_duplicates("symbol").copy()
    last["tv"] = last["symbol"].map(fnd.tv_key)
    m = last.merge(tv[["tv", "tv_open", "tv_high", "tv_low", "tv_close", "tv_volume"]], on="tv", how="inner")
    for c in ("tv_open", "tv_high", "tv_low", "tv_close", "tv_volume"):
        m[c] = pd.to_numeric(m[c], errors="coerce")
    liquid = m[(m["volume"] > 0) & (m["tv_volume"] > 0)]
    if liquid.empty:
        return df, None
    same = (liquid["tv_volume"].round() == liquid["volume"].round()).mean()
    if same > 0.3:  # TradingView still shows the last NSE session -> market holiday / not opened
        log(f"no new session on TradingView yet (same-volume share {same:.0%})")
        return df, None
    m = m[(m["tv_close"] > 0) & (m["tv_volume"] > 0)]
    chg = m["tv_close"] / m["close"]
    m = m[(chg > 0.75) & (chg < 1.33)]  # skip ex-split days until NSE's file arrives
    live = pd.DataFrame({
        "symbol": m["symbol"], "isin": m["isin"], "series": m["series"],
        "open": m["tv_open"], "high": m["tv_high"], "low": m["tv_low"], "close": m["tv_close"],
        "prev_close": m["close"], "volume": m["tv_volume"], "value": m["tv_volume"] * m["tv_close"],
        "trades": np.nan, "issue_size": m["issue_size"], "deliv_pct": np.nan, "date": today,
    })
    log(f"added {len(live)} provisional bars for {today.date()} from TradingView")
    return pd.concat([df, live], ignore_index=True), {"date": str(today.date()), "time": now.strftime("%H:%M")}


# --------------------------------------------------------------------------- periods & indicators
def period_key(dates: pd.Series, tf: str) -> np.ndarray:
    d = dates.to_numpy().astype("datetime64[D]").astype(np.int64)
    if tf == "D":
        return d
    if tf == "W":
        return d - (d + 3) % 7  # Monday-start weeks (1970-01-01 was a Thursday)
    y = dates.dt.year.to_numpy().astype(np.int64)
    mth = dates.dt.month.to_numpy().astype(np.int64)
    if tf == "M":
        return y * 12 + mth - 1
    if tf == "Q":
        return y * 4 + (mth - 1) // 3
    return y


def period_label(tf: str, key: int) -> str:
    if tf in ("D", "W"):
        d = dt.date(1970, 1, 1) + dt.timedelta(days=int(key))
        return d.strftime("%d %b %Y") if tf == "D" else "Week of " + d.strftime("%d %b %Y")
    if tf == "M":
        return dt.date(int(key) // 12, int(key) % 12 + 1, 1).strftime("%b %Y")
    if tf == "Q":
        return f"Q{int(key) % 4 + 1} {int(key) // 4}"
    return str(int(key))


def compute_periods(df: pd.DataFrame):
    """For every symbol and timeframe: previous period O/H/L/C and current period O/H/L."""
    latest = df["date"].max()
    out, labels = {}, {}
    for tf in TFS:
        key = period_key(df["date"], tf)
        cur = int(period_key(pd.Series([latest]), tf)[0])
        tmp = pd.DataFrame({"symbol": df["symbol"].to_numpy(), "key": key, "o": df["open"].to_numpy(),
                            "h": df["high"].to_numpy(), "l": df["low"].to_numpy(), "c": df["close"].to_numpy()})
        agg = tmp.groupby(["symbol", "key"], sort=True).agg(
            o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last")).reset_index()
        cur_df = agg[agg["key"] == cur].set_index("symbol")
        prev_df = agg[agg["key"] < cur].groupby("symbol").tail(1).set_index("symbol")
        out[tf] = (prev_df, cur_df)
        prev_keys = sorted(set(agg.loc[agg["key"] < cur, "key"]))
        labels[tf] = {"cur": period_label(tf, cur), "prev": period_label(tf, prev_keys[-1]) if prev_keys else None}
    return out, labels


def _rsi(c: np.ndarray, n: int = 14) -> float | None:
    if len(c) <= n:
        return None
    d = np.diff(c)
    up = pd.Series(np.clip(d, 0, None)).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    dn = pd.Series(np.clip(-d, 0, None)).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    if dn == 0:
        return 100.0
    return 100 - 100 / (1 + up / dn)


def _ema(c: np.ndarray, n: int) -> float | None:
    if len(c) < n:
        return None
    return float(pd.Series(c).ewm(span=n, adjust=False).mean().iloc[-1])


def symbol_stats(df: pd.DataFrame) -> pd.DataFrame:
    latest = df["date"].max()
    ytd_anchor = pd.Timestamp(latest.year - 1, 12, 31)
    rows = []
    for sym, g in df.groupby("symbol", sort=False):
        c = g["close"].to_numpy(dtype=float)
        h = g["high"].to_numpy(dtype=float)
        lo = g["low"].to_numpy(dtype=float)
        v = g["volume"].to_numpy(dtype=float)
        val = g["value"].to_numpy(dtype=float)
        dts = g["date"].to_numpy()
        last_dt = pd.Timestamp(dts[-1])
        yr = dts > np.datetime64(latest - pd.Timedelta(days=365))

        def ret(days=None, anchor=None):
            a = anchor if anchor is not None else latest - pd.Timedelta(days=days)
            idx = np.searchsorted(dts, np.datetime64(a), side="right") - 1
            if idx < 0 or c[idx] <= 0:
                return None
            return (c[-1] / c[idx] - 1) * 100

        prev = c[-2] if len(c) > 1 else np.nan
        rows.append({
            "symbol": sym,
            "last": last_dt.strftime("%Y-%m-%d"),
            "close": c[-1], "open": g["open"].iat[-1], "high": h[-1], "low": lo[-1],
            "prev": prev, "chg": (c[-1] / prev - 1) * 100 if prev and prev > 0 else None,
            "vol": v[-1],
            "vol_ratio": v[-1] / np.nanmean(v[-21:-1]) if len(v) > 5 and np.nanmean(v[-21:-1]) > 0 else None,
            "avg_val_cr": np.nanmean(val[-20:]) / 1e7 if len(val) else None,
            "deliv": next((x for x in g["deliv_pct"].to_numpy()[-2:][::-1] if np.isfinite(x)), None),
            "h52": np.nanmax(h[yr]) if yr.any() else None,
            "l52": np.nanmin(lo[yr]) if yr.any() else None,
            "rsi": _rsi(c), "ema20": _ema(c, 20), "ema50": _ema(c, 50), "ema200": _ema(c, 200),
            "r_1w": ret(7), "r_1m": ret(30), "r_3m": ret(91), "r_6m": ret(182), "r_1y": ret(365),
            "r_ytd": ret(anchor=ytd_anchor) if dts[0] <= np.datetime64(ytd_anchor) else None,
            "issue": g["issue_size"].ffill().iat[-1],
            "series": g["series"].iat[-1],
            "isin": g["isin"].iat[-1],
            "closes": [round(float(x), 2) for x in c[-11:]],
            "bars": len(c),
        })
    return pd.DataFrame(rows).set_index("symbol")


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--out", default=str(ROOT / "site" / "data.js"))
    ap.add_argument("--start", help="first date to keep, YYYY-MM-DD (default 1 Jan of previous year)")
    ap.add_argument("--now", help="pretend current IST time, e.g. 2026-11-20T20:00 (testing)")
    ap.add_argument("--no-download", action="store_true", help="do not contact NSE, use stored data")
    ap.add_argument("--no-fundamentals", action="store_true")
    ap.add_argument("--no-live", action="store_true", help="never add provisional intraday bars")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    now = (dt.datetime.fromisoformat(args.now).replace(tzinfo=IST) if args.now else dt.datetime.now(IST))
    today = now.date()
    start = dt.date.fromisoformat(args.start) if args.start else dt.date(today.year - 1, 1, 1)
    store = nse.EodStore(Path(args.data))
    session = nse.make_session()

    if not args.no_download:
        nse.update_store(store, start, today, session, workers=args.workers)
        pruned = store.prune(start)
        if pruned:
            log(f"pruned {pruned} file(s) older than {start}")
    ref = load_reference(session, store.meta, offline=args.no_download)

    raw = store.load(start)
    if raw.empty:
        log("ERROR: no NSE data in the store yet")
        return 1
    nse_date = raw["date"].max()
    log(f"history: {len(raw):,} rows, {raw['date'].nunique()} sessions, {raw['date'].min().date()} .. {nse_date.date()}")

    df = apply_symbol_changes(raw, ref["symchg"])
    df = link_by_isin(df)
    df["_p"] = df["series"].map(nse.SERIES_PRIORITY).fillna(9)
    df = df.sort_values(["symbol", "date", "_p"]).drop_duplicates(["symbol", "date"], keep="first").drop(columns="_p")
    df, events = adjust_corporate_actions(df)

    tv = None if args.no_fundamentals else fnd.fetch_tradingview(session)
    live = None
    if not args.no_live:
        df, live = add_live_bars(df, tv, now)
        df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    latest = df["date"].max()

    # ---- universe: NSE equities (+ SME), not ETFs, not rights entitlements, traded in last 60 days
    stats = symbol_stats(df)
    eq = ref["equity"]
    eq_map = eq.set_index("symbol") if eq is not None else pd.DataFrame()
    tvm = tv.set_index("tv") if tv is not None else pd.DataFrame()
    keep = []
    for sym, st in stats.iterrows():
        tvr = tvm.loc[fnd.tv_key(sym)] if fnd.tv_key(sym) in tvm.index else None
        tv_type = (tvr["tv_type"] if tvr is not None else None)
        if re.search(r"-RE\d*$", sym) or tv_type == "fund":
            continue
        if pd.Timestamp(st["last"]) < latest - pd.Timedelta(days=60):
            continue
        if sym in eq_map.index or st["series"] in SME_SERIES or tv_type == "stock":
            keep.append(sym)
    stats = stats.loc[keep]
    log(f"universe: {len(stats)} stocks")

    periods, labels = compute_periods(df[df["symbol"].isin(keep)])
    members, nse_industry = ref["index"]
    idx_of = {}
    for code, syms in members.items():
        for s in syms:
            idx_of.setdefault(s, []).append(code)
    ev_by_sym = {}
    for s, d, f in events.itertuples(index=False):
        ev_by_sym.setdefault(s, []).append(f"{pd.Timestamp(d):%d %b %Y} x{f:.4g}")

    # ---- assemble rows
    cols = ["sym", "name", "series", "sme", "fo", "idx", "sector", "industry", "nse_ind", "last",
            "close", "chg", "prev", "open", "high", "low", "vol", "vol_ratio", "avg_val_cr", "deliv",
            "h52", "l52", "rsi", "ema20", "ema50", "ema200",
            "r_1w", "r_1m", "r_3m", "r_6m", "r_1y", "r_ytd",
            "mcap", "shares_cr", "fv", "pe", "pb", "bv", "dy", "roce", "roe", "roe_ttm", "roic", "roa",
            "eps", "de", "sales_g", "profit_g", "eps_g", "sales_g_fy", "profit_g_fy", "peg", "pio",
            "mcap_sales", "cmp_fcf", "ind_pe", "holders", "opm", "npm", "cur_ratio", "int_cov",
            "ev_ebitda", "beta", "float_pct", "payout", "sales_cr", "profit_cr", "next_res", "listed", "ca"]
    recs = []
    for sym, st in stats.iterrows():
        t = tvm.loc[fnd.tv_key(sym)] if fnd.tv_key(sym) in tvm.index else None
        tg = (lambda k: (None if t is None else t.get(k)))
        e = eq_map.loc[sym] if sym in eq_map.index else None
        close = st["close"]
        eps, bv = num(tg("eps")), num(tg("bv"))
        issue = num(st["issue"]) or num(tg("tv_shares"))
        mcap = close * issue / 1e7 if issue else (num(tg("tv_mcap")) or 0) / 1e7 or None
        sales_cr = (num(tg("sales_ttm")) or 0) / 1e7 or None
        fcf = num(tg("fcf_ttm")) or num(tg("fcf_fy"))
        tv_close = num(tg("tv_close"))
        dy = num(tg("dy"))
        if dy is not None and tv_close and close:
            dy = dy * tv_close / close
        name = (e["company"] if e is not None else None) or ref["names"].get(sym) or tg("tv_desc") or sym
        nr = tg("next_res")
        recs.append({
            "sym": sym, "name": name, "series": st["series"], "sme": int(st["series"] in SME_SERIES),
            "fo": int(sym in ref["fo"]), "idx": ",".join(sorted(idx_of.get(sym, []))),
            "sector": tg("sector"), "industry": tg("industry"), "nse_ind": nse_industry.get(sym),
            "last": st["last"], "close": close, "chg": st["chg"], "prev": st["prev"], "open": st["open"],
            "high": st["high"], "low": st["low"], "vol": st["vol"], "vol_ratio": st["vol_ratio"],
            "avg_val_cr": st["avg_val_cr"], "deliv": st["deliv"],
            "h52": st["h52"], "l52": st["l52"], "rsi": st["rsi"],
            "ema20": st["ema20"], "ema50": st["ema50"], "ema200": st["ema200"],
            "r_1w": st["r_1w"], "r_1m": st["r_1m"], "r_3m": st["r_3m"], "r_6m": st["r_6m"],
            "r_1y": st["r_1y"], "r_ytd": st["r_ytd"],
            "mcap": mcap, "shares_cr": issue / 1e7 if issue else None,
            "fv": num(e["fv"]) if e is not None else None,
            "pe": close / eps if eps and eps > 0 else None,
            "pb": close / bv if bv and bv > 0 else None, "bv": bv, "dy": dy,
            "roce": tg("roce"), "roe": tg("roe"), "roe_ttm": tg("roe_ttm"), "roic": tg("roic"), "roa": tg("roa"),
            "eps": eps, "de": tg("de"), "sales_g": tg("sales_g"), "profit_g": tg("profit_g"), "eps_g": tg("eps_g"),
            "sales_g_fy": tg("sales_g_fy"), "profit_g_fy": tg("profit_g_fy"), "peg": tg("peg"), "pio": tg("pio"),
            "mcap_sales": mcap / sales_cr if mcap and sales_cr and sales_cr > 0 else None,
            "cmp_fcf": mcap / (fcf / 1e7) if mcap and fcf and fcf > 0 else None,
            "ind_pe": None, "holders": tg("holders"), "opm": tg("opm"), "npm": tg("npm"),
            "cur_ratio": tg("cur_ratio"), "int_cov": tg("int_cov"), "ev_ebitda": tg("ev_ebitda"),
            "beta": tg("beta"), "float_pct": tg("float_pct"), "payout": tg("payout"),
            "sales_cr": sales_cr, "profit_cr": (num(tg("profit_ttm")) or 0) / 1e7 or None,
            "next_res": (dt.datetime.fromtimestamp(nr, IST).strftime("%Y-%m-%d") if num(nr) else None),
            "listed": e["listed"] if e is not None else None,
            "ca": "; ".join(ev_by_sym.get(sym, [])[-3:]) or None,
        })
    frame = pd.DataFrame(recs)
    # Industry P/E = median P/E of profitable peers in the same industry
    pe_ok = frame[(frame["pe"] > 0) & (frame["pe"] < 1000)]
    med = pe_ok.groupby("industry")["pe"].median()
    cnt = pe_ok.groupby("industry")["pe"].count()
    frame["ind_pe"] = frame["industry"].map(med.where(cnt >= 3))

    text_cols = {"sym", "name", "series", "idx", "sector", "industry", "nse_ind", "last", "next_res", "listed", "ca"}
    int_cols = {"sme", "fo", "vol", "holders", "pio"}
    rows = []
    for rec in frame[cols].itertuples(index=False):
        row = []
        for c, v in zip(cols, rec):
            if c in text_cols:
                row.append(None if v is None or (isinstance(v, float) and math.isnan(v)) or v == "" else str(v))
            elif c in int_cols:
                f = num(v)
                row.append(None if f is None else int(round(f)))
            elif c in ("mcap", "sales_cr", "profit_cr"):
                row.append(rnd(v, 0))
            else:
                row.append(rnd(v, 2))
        rows.append(row)

    piv = {}
    order = frame["sym"].tolist()
    for tf in TFS:
        prev_df, cur_df = periods[tf]
        arr = []
        for s in order:
            p = prev_df.loc[s] if s in prev_df.index else None
            q = cur_df.loc[s] if s in cur_df.index else None
            arr.append([None if p is None else rnd(p[k]) for k in "ohlc"] +
                       [None if q is None else rnd(q[k]) for k in "ohl"])
        piv[tf] = arr
    closes = [stats.loc[s, "closes"] for s in order]

    payload = {
        "meta": {
            "generated": now.strftime("%Y-%m-%d %H:%M IST") if args.now else dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
            "nse_date": f"{nse_date:%Y-%m-%d}",
            "latest": f"{latest:%Y-%m-%d}",
            "live": live,
            "fundamentals": tv is not None,
            "count": len(rows),
            "periods": labels,
            "history_from": f"{raw['date'].min():%Y-%m-%d}",
            "sources": {
                "prices": "NSE official EOD bhavcopy (nsearchives.nseindia.com)",
                "shares": "NSE PR bundle MCAP file",
                "fundamentals": "TradingView screener feed (unofficial)" if tv is not None else None,
                "live": "TradingView (approx. 15-min delayed)" if live else None,
            },
        },
        "cols": cols,
        "rows": rows,
        "piv": piv,
        "cl": closes,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    js = "window.SCREENER_DATA=" + json.dumps(payload, separators=(",", ":"), allow_nan=False) + ";\n"
    out.write_text(js, encoding="utf-8")
    log(f"wrote {out} ({len(js) / 1e6:.2f} MB, {len(rows)} stocks, NSE date {nse_date.date()}"
        + (f", live {live['date']} {live['time']}" if live else "") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
