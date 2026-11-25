"""
Build the data files for the Smart Money & Institutional page (site/smartmoney.html).

    python scripts/build_smartmoney.py                 # download new NSE files, then build
    python scripts/build_smartmoney.py --no-download   # rebuild from stored data only

Writes next to the page:
  site/smartmoney-data.js     stock table, SMF sheet (last 60 sessions), setups, post-move statistics,
                              F&O build-up, bulk/block deals, FII/DII positioning, market series
  site/sm-hist/hNN.js         per-stock daily history for the detail chart (64 shards, loaded on demand)

Methods (details in the page's Methodology tab):
  * SMF score ("smart money footprint", same scale as the SMF sheet: ~3 normal, >=3.5 favourable, >=8 sharp):
        SMF = 3.13 * (DPT / mean(DPT over the previous 20 sessions)) ** 0.571
    where DPT = deliverable quantity / number of trades (average delivery-backed order size).
    Calibrated on the SMF demo sheet (Mar-Apr 2025, 5,366 stock-days): R^2 = 0.945 in log terms.
    Without a delivery figure the fallback uses volume per trade: 3.05 * (VPT / mean20) ** 0.678 (R^2 = 0.74).
  * Footprint bias (buy / sell side of a footprint day): close location in the day's range + close vs VWAP.
  * Institutional Score 0-100: delivery accumulation, footprint balance, money flow, trend/RS, F&O OI and
    institutional bulk/block deals (weights re-normalised when a component has no data).
  * Setups (Wyckoff, O'Neil/Kacher pocket pivots, Minervini VCP, Stockbee episodic pivots, F&O build-ups, ...)
    and their post-move statistics over the stored history.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import warnings
import zlib
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_data as bd  # noqa: E402
import build_rotation as br  # noqa: E402
import fundamentals as fnd  # noqa: E402
import nse_data as nse  # noqa: E402
import nse_indices as ni  # noqa: E402
import nse_smartmoney as sm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")
SME_SERIES = {"SM", "ST"}
warnings.filterwarnings("ignore", message="(Mean of empty slice|All-NaN slice encountered|invalid value encountered)")

SMF_A = (3.13, 0.571)       # delivery-per-trade model (calibrated, see docstring)
SMF_B = (3.05, 0.678)       # volume-per-trade fallback
SMF_N = 20
SHEET_SESSIONS = 60
HIST_SESSIONS = 250
MARKET_SESSIONS = 250
SHARDS = 128
LIQ_VALUE = 1e7             # Rs 1 crore average daily traded value for statistics / default screens
HORIZONS = (1, 3, 5, 10, 20)

# cumulative share of a day's volume traded by minutes after 09:15 (typical NSE U-shaped profile)
VOL_CURVE = [(0, 0), (15, .10), (30, .17), (60, .27), (90, .35), (120, .42), (150, .48), (180, .54),
             (210, .60), (240, .66), (270, .73), (300, .80), (330, .88), (360, .96), (375, 1.0)]

SETUPS = [  # code, label, side, description (rules are in setups())
    ("INFLOW", "SMF gradual inflow", "bull", "3 sessions in a row with SMF ≥ 3.5 and the latest higher than two days ago (e.g. 3.5 → 4.0 → 4.5)."),
    ("SPIKE", "SMF spike ≥ 8", "bull", "Very large delivery-backed orders: SMF ≥ 8. Often precedes short, sharp moves."),
    ("SMFBUY", "Footprint with buy bias", "bull", "SMF ≥ 5 and the day closed in the upper part of its range, above VWAP (buy-side footprint)."),
    ("ABSORB", "Absorption (effort > result)", "bull", "SMF ≥ 3.5, volume ≥ 1.5x its 20-day average but range ≤ 0.8x average, close at/above VWAP (Wyckoff absorption)."),
    ("RETEST", "Retest of SMF demand zone", "bull", "Price ran ≥ 5% above a buy-side SMF zone (SMF ≥ 5) and has come back into the zone without closing below it."),
    ("POCKET", "Pocket pivot", "bull", "Up day whose volume beats every down-day volume of the last 10 sessions, close above the 10- and 50-day averages (Kacher & Morales)."),
    ("DLVBO", "Delivery breakout", "bull", "Close above the 55-day high with volume ≥ 1.5x and delivery qty ≥ 1.5x their 20-day averages."),
    ("QUIET", "Quiet delivery accumulation", "bull", "10-day average delivery % ≥ 1.25x the prior 60 days while price holds a ≤ 15% range and is within 5% of where it was 20 sessions ago."),
    ("SPRING", "Wyckoff spring", "bull", "Undercut of the 40-day low that closes back above it in the upper part of the range on ≥ 1.3x volume."),
    ("VCP", "Volatility contraction (VCP)", "bull", "Within 10% of the 52-week high, ATR(10) ≤ 0.65x ATR(50), 10-day volume ≤ 0.75x 50-day, close > 50-DMA > 200-DMA (Minervini)."),
    ("EP", "Episodic pivot (gap on volume)", "bull", "Gap up ≥ 4%, close ≥ 4% up and near the high, volume ≥ 3x its 20-day average (Stockbee)."),
    ("NR7", "NR7 coil with footprint", "bull", "Narrowest range of the last 7 sessions on a day with SMF ≥ 3.5."),
    ("LONGBU", "Long build-up (F&O)", "bull", "Price +1% or more, futures OI +5% or more and delivery qty above its 20-day average."),
    ("SHORTCOV", "Short covering (F&O)", "bull", "Price +2% or more while futures OI falls 5% or more."),
    ("INSTBUY", "Institutional bulk/block buy", "bull", "Net buying by mutual funds, insurers, FPIs, AIFs or banks in bulk/block deals (intraday round-trips excluded)."),
    ("SMFSELL", "Footprint with sell bias", "bear", "SMF ≥ 5 and the day closed in the lower part of its range, below VWAP (distribution footprint)."),
    ("UPTHRUST", "Wyckoff upthrust", "bear", "Poke above the 40-day high that closes back below it in the lower part of the range on ≥ 1.3x volume."),
    ("SHORTBU", "Short build-up (F&O)", "bear", "Price -1% or more with futures OI +5% or more."),
]
SETUP_CODES = [s[0] for s in SETUPS]
NEEDS_DELIV = {"INFLOW", "SPIKE", "SMFBUY", "SMFSELL", "ABSORB", "DLVBO", "QUIET", "NR7", "LONGBU", "RETEST"}

DEAL_CLASSES = [  # (code, label, regex) - first match wins
    ("PROP", "Prop / HFT", r"JUMP TRADING|QE SECURITIES|HRTI|GRAVITON|ALPHAGREP|MUSIGMA|JUNOMONETA|NK SECURITIES|"
                           r"SILVERLEAF|TOWER RESEARCH|IRAGE|QICAP|MICROCURVES|DOVETAIL|PACE STOCK|ELIXIR WEALTH|"
                           r"SHARE INDIA|GIRIRAJ|NEXT ORBIT|ESTEE ADVISORS|SUMMIT SECURITIES|HFT|ARBITRAGE"),
    ("MF", "Mutual fund", r"MUTUAL FUND|\bMF\b|ASSET MANAGEMENT|\bAMC\b|TRUSTEE CO"),
    ("INS", "Insurance", r"INSURANCE|LIFE INSURANCE CORPORATION|\bLIC\b"),
    ("FPI", "FPI / foreign", r"GOLDMAN|MORGAN STANLEY|SOCIETE GENERALE|BNP PARIBAS|CITIGROUP|NOMURA|MERRILL|BOFA|"
                             r"J\.? ?P\.? ?MORGAN|\bUBS\b|BARCLAYS|HSBC|COPTHALL|VANGUARD|BLACKROCK|ABU DHABI|"
                             r"GOVERNMENT OF SINGAPORE|NORGES|MONETARY AUTHORITY|CLSA|MACQUARIE|FIDELITY|INVESCO|"
                             r"NATIXIS|DEUTSCHE|CREDIT SUISSE|SMALLCAP WORLD|ISHARES|AMUNDI|SCHRODER|ABERDEEN|"
                             r"\bPLC\b|\bPTE\b|\bLLC\b|\bL\.P\b|\bLP\b|\bINC\b|\bS\.A\b|MAURITIUS|CAYMAN|\bSICAV\b|\bFCP\b"),
    ("AIF", "AIF / fund / PMS", r"\bAIF\b|ALTERNATIVE|\bFUND\b|\bFUNDS\b|VENTURE|PORTFOLIO|CAPITAL ADVISORS|\bPMS\b|"
                                r"INVESTMENT MANAGERS|OPPORTUNIT"),
    ("BANK", "Bank / FI", r"\bBANK\b|FINANCIAL INSTITUTION|STATE BANK"),
    ("CORP", "Corporate", r"PRIVATE LIMITED|PVT\.? LTD|\bLIMITED\b|\bLTD\b|\bLLP\b|\bCORPORATION\b|\bHUF\b|TRUST"),
]
INST_CLASSES = {"MF", "INS", "FPI", "AIF", "BANK"}


def log(msg: str) -> None:
    print(f"[smartmoney] {msg}", flush=True)


# --------------------------------------------------------------------------- array helpers
def roll(a: np.ndarray, n: int, fn: str = "mean", minp: int | None = None, shift: int = 0) -> np.ndarray:
    df = pd.DataFrame(a)
    if shift:
        df = df.shift(shift)
    r = df.rolling(n, min_periods=minp if minp is not None else n)
    return getattr(r, fn)().to_numpy()


def shift(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(a, np.nan, dtype=float)
    if k > 0:
        out[k:] = a[:-k]
    elif k < 0:
        out[:k] = a[-k:]
    else:
        out[:] = a
    return out


def div(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        q = np.true_divide(np.asarray(a, dtype=float), np.asarray(b, dtype=float))
    return np.where(np.isfinite(q), q, np.nan)


def clip(a, lo=0.0, hi=100.0):
    return np.clip(a, lo, hi)


def first_trigger(cond: np.ndarray, cooldown: int) -> np.ndarray:
    """True where cond is True and it was not True in the previous `cooldown` sessions."""
    c = np.nan_to_num(cond.astype(float))
    prev = roll(c, cooldown, "max", minp=1, shift=1)
    return (c > 0) & ~(np.nan_to_num(prev) > 0)


def wnanmean(parts: list[tuple[float, np.ndarray]], min_w: float = 0.5) -> np.ndarray:
    num = np.zeros_like(parts[0][1], dtype=float)
    den = np.zeros_like(parts[0][1], dtype=float)
    for w, x in parts:
        ok = np.isfinite(x)
        num += np.where(ok, w * x, 0)
        den += np.where(ok, w, 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(den >= min_w - 1e-9, num / den, np.nan)


def r(v, nd=2):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, nd)


def vol_fraction(minutes_since_open: float) -> float:
    m = max(0.0, min(375.0, minutes_since_open))
    for (m0, f0), (m1, f1) in zip(VOL_CURVE, VOL_CURVE[1:]):
        if m <= m1:
            return max(0.05, f0 + (f1 - f0) * (m - m0) / (m1 - m0))
    return 1.0


def classify_client(name: str) -> str:
    n = str(name or "").upper()
    for code, _lbl, rx in DEAL_CLASSES:
        if re.search(rx, n):
            return code
    return "IND"


# --------------------------------------------------------------------------- history
def load_history(store, session, start, now, args, ref):
    raw = store.load(start)
    if raw.empty:
        return None
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
    df, _ev = bd.adjust_corporate_actions(df, unknown, bd.load_ca_notices(store.meta))
    tv = None if args.no_download else fnd.fetch_tradingview(session)
    tv = br.load_or_cache_tv_classes(store.meta, tv)
    live = None
    if not args.no_live and not args.no_download and tv is not None and "tv_close" in tv.columns:
        df, live = bd.add_live_bars(df, tv, now)
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    return df, live, nse_date, tv


# --------------------------------------------------------------------------- main
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
    start = dt.date(today.year - 1, 1, 1)
    data_dir = Path(args.data)
    store = nse.EodStore(data_dir)
    sms = sm.SmStore(data_dir)
    session = nse.make_session()

    sessions = sorted(d for d in store.existing_dates() if d >= start)
    if not args.no_download:
        sm.update_all(sms, sessions, today, session, deliv_from=start, workers=args.workers)
        sms.prune(start)

    ref = bd.load_reference(session, store.meta, offline=True)
    loaded = load_history(store, session, start, now, args, ref)
    if loaded is None:
        log("ERROR: no NSE stock data in the store")
        return 1
    df, live, nse_date, tv = loaded

    # ---- universe (screener rules) ------------------------------------------------------------
    eq = ref["equity"]
    eq_syms = set(eq["symbol"]) if eq is not None else set()
    eq_name = dict(zip(eq["symbol"], eq["company"])) if eq is not None else {}
    tv_type = dict(zip(tv["tv"], tv["tv_type"])) if tv is not None and "tv_type" in tv.columns else {}
    latest = df["date"].max()
    last_seen = df.groupby("symbol")["date"].max()
    nb = df.groupby("symbol")["date"].count()
    series_last = df.groupby("symbol")["series"].last()
    keep = []
    for sym, last_d in last_seen.items():
        tt = tv_type.get(fnd.tv_key(sym))
        if re.search(r"-RE\d*$", sym) or tt == "fund":
            continue
        if last_d < latest - pd.Timedelta(days=20) or nb[sym] < 25:
            continue
        if sym in eq_syms or series_last[sym] in SME_SERIES or tt == "stock":
            keep.append(sym)
    df = df[df["symbol"].isin(set(keep))]
    syms = sorted(keep)
    dates = pd.DatetimeIndex(sorted(df["date"].unique()))
    T, N = len(dates), len(syms)
    col_of = {s: i for i, s in enumerate(syms)}
    t_eod = int(np.searchsorted(dates.values, np.datetime64(nse_date)))  # last session with NSE files
    is_live = live is not None and t_eod < T - 1
    log(f"universe: {N} stocks x {T} sessions {dates[0].date()} .. {dates[-1].date()}"
        + (f" (live {live['time']})" if is_live else ""))

    def wide(frame, col):
        return (frame.pivot(index="date", columns="symbol", values=col)
                .reindex(index=dates, columns=syms).to_numpy(dtype=float))

    O, H, L, C = wide(df, "open"), wide(df, "high"), wide(df, "low"), wide(df, "close")
    V, VAL, TRD = wide(df, "volume"), wide(df, "value"), wide(df, "trades")
    ISS = pd.DataFrame(wide(df, "issue_size")).ffill().bfill().to_numpy()
    traded = np.isfinite(C) & (np.nan_to_num(V) > 0)
    Cf = pd.DataFrame(C).ffill().to_numpy()
    Hf = np.where(traded, H, Cf)
    Lf = np.where(traded, L, Cf)
    Cp = shift(Cf, 1)
    nbars = np.cumsum(traded, axis=0)
    sme = np.array([series_last.get(s) in SME_SERIES for s in syms])
    ser_last = np.array([series_last.get(s) for s in syms], dtype=object)

    # ---- delivery (sm store), adjusted to the split/bonus-adjusted price & volume scale ----------
    dl = sms.load_deliv(start)
    if len(dl):
        dl = bd.apply_symbol_changes(dl, ref["symchg"])
        dl["_p"] = dl["series"].map(nse.SERIES_PRIORITY).fillna(9)
        dl = dl.sort_values(["symbol", "date", "_p"]).drop_duplicates(["symbol", "date"]).drop(columns="_p")
        dl = dl[dl["symbol"].isin(col_of)]
        DP, APr, DCL = wide(dl, "dp"), wide(dl, "ap"), wide(dl, "close")
        dser = dl.pivot(index="date", columns="symbol", values="series").reindex(index=dates, columns=syms).to_numpy(dtype=object)
        tft = np.isin(dser, ["BE", "BZ"]) & np.isnan(DP) & traded  # trade-for-trade: compulsory delivery
        DP = np.where(tft, 100.0, DP)
        AP = APr * div(C, DCL)                                     # VWAP on the adjusted price scale
        deliv_days = int(np.isfinite(DP[: t_eod + 1]).any(axis=1).sum())
    else:
        DP = np.full((T, N), np.nan)
        AP = np.full((T, N), np.nan)
        deliv_days = 0
    DPe = wide(df, "deliv_pct")  # the screener's own store has delivery % for its most recent sessions
    DP = np.where(np.isfinite(DP), DP, np.where(traded, DPe, np.nan))
    log(f"delivery history: {deliv_days} session(s)")
    DQ = DP / 100.0 * V

    # ---- live projection of today's volume (TradingView is ~15 min delayed) --------------------
    Vp = V.copy()
    vfrac = 1.0
    if is_live:
        hh, mm = map(int, live["time"].split(":"))
        vfrac = vol_fraction((hh * 60 + mm - 15) - (9 * 60 + 15))
        Vp[T - 1] = V[T - 1] / vfrac

    # ---- core features --------------------------------------------------------------------------
    rng = Hf - Lf
    ret = div(Cf, Cp) - 1
    rng_ok = np.where(rng > 0, rng, Cf * 0.001)
    adrp = roll(div(rng_ok, Cf), 20, minp=10, shift=1)
    V20 = roll(V, 20, minp=10, shift=1)
    VR20 = div(Vp, V20)
    RR20 = div(rng_ok, roll(rng_ok, 20, minp=10, shift=1))
    DQ20 = roll(DQ, 20, minp=10, shift=1)
    DR20 = div(DQ, DQ20)
    VAL20 = roll(VAL, 20, minp=5)
    TRDok = np.where(TRD > 0, TRD, np.nan)
    DPT = div(DQ, TRDok)
    VPT = div(V, TRDok)
    with np.errstate(invalid="ignore"):
        smf_a = SMF_A[0] * np.power(div(DPT, roll(DPT, SMF_N, minp=10, shift=1)), SMF_A[1])
        smf_b = SMF_B[0] * np.power(div(VPT, roll(VPT, SMF_N, minp=10, shift=1)), SMF_B[1])
    SMF = np.where(np.isfinite(smf_a), smf_a, smf_b)
    SMF = np.where(traded, np.clip(SMF, 0, 99), np.nan)
    SRC = np.where(np.isfinite(smf_a) & traded, 1, np.where(np.isfinite(SMF), 2, 0))
    clv = np.where(rng > 0, div((Cf - Lf) - (Hf - Cf), rng), np.sign(np.nan_to_num(ret)))
    vw = np.clip(div(div(Cf, AP) - 1, np.maximum(0.25 * adrp, 0.002)), -1, 1)
    BIAS = np.where(np.isfinite(vw), 0.6 * clv + 0.4 * vw, clv)
    BIAS = np.where(traded, np.clip(BIAS, -1, 1), np.nan)

    SMA10, SMA50, SMA200 = roll(Cf, 10), roll(Cf, 50, minp=40), roll(Cf, 200, minp=150)
    H52 = roll(Hf, 250, "max", minp=60)
    trng = np.fmax(Hf, Cp) - np.fmin(Lf, Cp)
    ATR10, ATR50 = roll(trng, 10), roll(trng, 50, minp=40)
    V10, V50 = roll(V, 10, minp=8), roll(V, 50, minp=40)
    H55p, H40p, L40p = roll(Hf, 55, "max", minp=40, shift=1), roll(Hf, 40, "max", minp=30, shift=1), roll(Lf, 40, "min", minp=30, shift=1)
    LIQ = (np.nan_to_num(VAL20) >= LIQ_VALUE) & (np.nan_to_num(Cf) >= 10) & (nbars >= 60) & ~sme[None, :]

    # ---- F&O open interest ------------------------------------------------------------------------
    fo = sms.load_fo(start)
    fo_syms = set(ref.get("fo") or [])
    OIC = np.full((T, N), np.nan)    # daily % change of non-expiring futures OI
    OI = np.full((T, N), np.nan)
    PCR = np.full((T, N), np.nan)
    idx_fo = pd.DataFrame()
    if len(fo):
        fs = fo[fo["kind"] == "S"].copy()
        fs = bd.apply_symbol_changes(fs, ref["symchg"])
        fs = fs[fs["symbol"].isin(col_of)].drop_duplicates(["symbol", "date"])
        if len(fs):
            OI = wide(fs, "fut_oi")
            OIC = div(wide(fs, "fut_doi_nx"), wide(fs, "fut_oi_nx") - wide(fs, "fut_doi_nx"))
            PCR = div(wide(fs, "pe_oi"), wide(fs, "ce_oi"))
            fo_syms |= set(fs.loc[fs["date"] == fs["date"].max(), "symbol"])
        idx_fo = fo[fo["kind"] == "I"]
    OIC = np.clip(OIC, -0.9, 5)
    with np.errstate(invalid="ignore"):
        lg = np.log1p(np.nan_to_num(OIC))
    OIC5 = np.expm1(roll(lg, 5, "sum", minp=3)) * np.where(np.isfinite(OIC), 1, np.nan)
    OIC20 = np.expm1(roll(lg, 20, "sum", minp=10)) * np.where(np.isfinite(OIC), 1, np.nan)
    log(f"F&O history: {fo['date'].nunique() if len(fo) else 0} session(s)")

    # ---- bulk / block deals -------------------------------------------------------------------------
    deals = sms.load_deals()
    DEALNET = np.zeros((T, N))
    deal_rows = []
    if len(deals):
        deals = deals.copy()
        deals["date"] = pd.to_datetime(deals["date"], errors="coerce")
        deals = deals.dropna(subset=["date"])
        deals = bd.apply_symbol_changes(deals, ref["symchg"])
        deals["cls"] = deals["client"].map(classify_client)
        both = deals.groupby(["date", "symbol", "client"])["side"].transform("nunique") > 1
        deals["intraday"] = both
        deals["val"] = deals["qty"] * deals["price"]
        deals["sgn"] = np.where(deals["side"] == "B", 1, -1)
        inst = deals[deals["cls"].isin(INST_CLASSES) & ~deals["intraday"] & deals["symbol"].isin(col_of)]
        if len(inst):
            agg = (inst.assign(nv=inst["val"] * inst["sgn"]).groupby(["date", "symbol"])["nv"].sum().reset_index())
            pos = dates.get_indexer(agg["date"])
            for (p, s, v) in zip(pos, agg["symbol"], agg["nv"]):
                if p >= 0:
                    DEALNET[p, col_of[s]] += v
        deal_rows = deals
    log(f"deals stored: {len(deals)}")

    # ---- RS rating (IBD style, percentile vs main-board stocks) ---------------------------------
    legs = [(63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)]
    num = np.zeros_like(Cf)
    den = np.zeros_like(Cf)
    for k, w in legs:
        leg = div(Cf, shift(Cf, k))
        ok = np.isfinite(leg) & (nbars > k)
        num += np.where(ok, w * leg, 0)
        den += np.where(ok, w, 0)
    rsraw = np.where(den >= 0.4 - 1e-9, div(num, den), np.nan)
    RS = np.full_like(rsraw, np.nan)
    for t in range(max(0, T - 70), T):
        s_t = rsraw[t]
        refv = np.sort(s_t[~sme & np.isfinite(s_t)])
        if len(refv) < 50:
            continue
        fin = np.isfinite(s_t)
        RS[t, fin] = np.clip(np.floor(np.searchsorted(refv, s_t[fin], side="right") / len(refv) * 98.999) + 1, 1, 99)

    # ---- Institutional score (0-100) -----------------------------------------------------------------
    DP10 = roll(DP, 10, minp=6)
    DP60p = roll(DP, 60, minp=30, shift=10)
    D1 = clip(50 + 125 * np.log(div(DP10, DP60p)))
    up = np.nan_to_num(DQ) * (ret > 0)
    dn = np.nan_to_num(DQ) * (ret < 0)
    eps = np.nan_to_num(DQ20) * 0.5 + 1
    D2 = clip(50 + 40 * np.log(div(roll(up, 20, "sum", minp=10) + eps, roll(dn, 20, "sum", minp=10) + eps)))
    D2 = np.where(np.isfinite(DP10), D2, np.nan)
    Dc = wnanmean([(1, D1), (1, D2)])
    fnet = roll(np.nan_to_num(np.maximum(SMF - 3, 0) * BIAS), 20, "sum", minp=5)
    Fc = np.where(np.isfinite(roll(SMF, 20, minp=5)), 50 + 50 * np.tanh(fnet / 3), np.nan)
    cmf = div(roll(np.nan_to_num(clv * V), 20, "sum", minp=10), roll(np.nan_to_num(V), 20, "sum", minp=10))
    M1 = clip(50 + 200 * cmf)
    uv = roll(np.nan_to_num(V) * (ret > 0), 50, "sum", minp=20)
    dv = roll(np.nan_to_num(V) * (ret < 0), 50, "sum", minp=20)
    M2 = clip(50 + 45 * np.log(div(uv + 1, dv + 1)))
    Mc = wnanmean([(1, M1), (1, M2)])
    trend = ((Cf > SMA50).astype(float) + (SMA50 > SMA200) + (Cf > SMA200) + (Cf >= 0.75 * H52)) * 25
    trend = np.where(np.isfinite(SMA200), trend, np.where(np.isfinite(SMA50), ((Cf > SMA50) * 2 + (Cf >= 0.75 * H52) * 2) * 25.0, np.nan))
    Tc = wnanmean([(1, RS), (1, trend)])
    pr20 = div(Cf, shift(Cf, 20)) - 1
    I_fo = np.where(OIC20 >= 0.05, np.where(pr20 > 0, 78, 22), np.where(OIC20 <= -0.05, np.where(pr20 > 0, 60, 40), 50.0))
    I_fo = np.where(np.isfinite(OIC20), I_fo, np.nan)
    dn60 = roll(DEALNET, 60, "sum", minp=1)
    any60 = roll((DEALNET != 0).astype(float), 60, "sum", minp=1)
    I_dl = np.where(np.nan_to_num(any60) > 0, 50 + 50 * np.tanh(div(dn60, np.nan_to_num(VAL20) * 5 + 1)), np.nan)
    Ic = wnanmean([(1, I_fo), (1, I_dl)])
    INST = wnanmean([(0.25, Dc), (0.25, Fc), (0.20, Mc), (0.15, Tc), (0.15, Ic)], min_w=0.35)
    INST = np.where(nbars >= 20, INST, np.nan)

    # ---- SMF zones (footprint days) + retest signals ----------------------------------------------
    zone_ev = (np.nan_to_num(SMF) >= 5) & traded
    RETEST = np.zeros((T, N), dtype=bool)
    zones_of = {}
    for i in range(N):
        zt = np.flatnonzero(zone_ev[:, i])
        zl = []
        for t in zt:
            lo, hi = Lf[t, i], Hf[t, i]
            vwap = AP[t, i] if np.isfinite(AP[t, i]) else (hi + lo + Cf[t, i]) / 3
            b = BIAS[t, i]
            zl.append((t, lo, hi, vwap, SMF[t, i], b))
            if b >= 0.3:  # buy-side zone: look for the first retest after a 5% run-up
                ran = False
                for u in range(t + 1, min(T, t + 61)):
                    if Hf[u, i] >= hi * 1.05:
                        ran = True
                    elif ran and Lf[u, i] <= hi and Cf[u, i] >= lo:
                        RETEST[u, i] = True
                        break
                    if Cf[u, i] < lo:
                        break
        zones_of[i] = zl[-6:]

    # ---- setups -----------------------------------------------------------------------------------------
    s1, s2 = shift(SMF, 1), shift(SMF, 2)
    up_day = ret > 0
    dn_vol = np.where(ret < 0, V, 0)
    maxdn10 = roll(np.nan_to_num(dn_vol), 10, "max", minp=5, shift=1)
    rng7min = roll(rng_ok, 7, "min", minp=7)
    hi20, lo20 = roll(Hf, 20, "max", minp=15), roll(Lf, 20, "min", minp=15)
    with np.errstate(invalid="ignore"):
        conds = {
            "INFLOW": (SMF >= 3.5) & (s1 >= 3.5) & (s2 >= 3.5) & (SMF > s2),
            "SPIKE": SMF >= 8,
            "SMFBUY": (SMF >= 5) & (BIAS >= 0.3),
            "SMFSELL": (SMF >= 5) & (BIAS <= -0.3),
            "ABSORB": (SMF >= 3.5) & (VR20 >= 1.5) & (RR20 <= 0.8) & (Cf >= np.where(np.isfinite(AP), AP, Cf)),
            "RETEST": RETEST,
            "POCKET": up_day & (Vp > maxdn10) & (Cf > SMA10) & (Cf > SMA50) & (Cf >= 0.85 * H52),
            "DLVBO": (Cf > H55p) & (VR20 >= 1.5) & (DR20 >= 1.5),
            "QUIET": (div(DP10, DP60p) >= 1.25) & (np.abs(pr20) <= 0.05) & (div(hi20, lo20) - 1 <= 0.15),
            "SPRING": (Lf < L40p) & (Cf > L40p) & (clv >= 0.3) & (VR20 >= 1.3),
            "UPTHRUST": (Hf > H40p) & (Cf < H40p) & (clv <= -0.3) & (VR20 >= 1.3),
            "VCP": (Cf >= 0.9 * H52) & (div(ATR10, ATR50) <= 0.65) & (div(V10, V50) <= 0.75) & (Cf > SMA50) & (SMA50 > SMA200),
            "EP": (div(O, Cp) >= 1.04) & (ret >= 0.04) & (clv >= 0.3) & (VR20 >= 3),
            "NR7": (rng_ok <= rng7min * 1.0000001) & (SMF >= 3.5),
            "LONGBU": (ret >= 0.01) & (OIC >= 0.05) & (DR20 >= 1),
            "SHORTCOV": (ret >= 0.02) & (OIC <= -0.05),
            "SHORTBU": (ret <= -0.01) & (OIC >= 0.05),
            "INSTBUY": DEALNET > 0,
        }
    cool = {"INFLOW": 5, "SPIKE": 5, "SMFBUY": 5, "SMFSELL": 5, "ABSORB": 5, "RETEST": 10, "POCKET": 3, "DLVBO": 10,
            "QUIET": 10, "SPRING": 10, "UPTHRUST": 10, "VCP": 10, "EP": 10, "NR7": 5, "LONGBU": 3, "SHORTCOV": 3,
            "SHORTBU": 3, "INSTBUY": 1}
    SIG = {k: first_trigger(np.nan_to_num(conds[k].astype(float)) > 0, cool[k]) & traded for k in SETUP_CODES}
    if is_live:  # delivery / OI are published after the close: those setups only run on NSE's EOD files
        for k in NEEDS_DELIV | {"LONGBU", "SHORTCOV", "SHORTBU", "INSTBUY"}:
            if k != "RETEST":
                SIG[k][T - 1] = False

    # ---- forward returns, MFE / MAE -----------------------------------------------------------------
    Cs = Cf.copy()
    Cs[t_eod + 1:] = np.nan               # live bar never counts as an outcome
    Hs = Hf.copy(); Hs[t_eod + 1:] = np.nan
    Ls = Lf.copy(); Ls[t_eod + 1:] = np.nan
    FWD = {k: div(shift(Cs, -k), Cf) - 1 for k in HORIZONS}
    fh = pd.DataFrame(Hs[::-1]).rolling(20, min_periods=1).max().to_numpy()[::-1]
    fl = pd.DataFrame(Ls[::-1]).rolling(20, min_periods=1).min().to_numpy()[::-1]
    MFE = div(shift(fh, -1), Cf) - 1
    MAE = div(shift(fl, -1), Cf) - 1
    complete20 = np.zeros(T, dtype=bool)
    complete20[: max(0, t_eod - 19)] = True

    def hit_first(t, i, up=0.10, down=0.05, n=20):
        c0 = Cf[t, i]
        for u in range(t + 1, min(t_eod, t + n) + 1):
            if Ls[u, i] <= c0 * (1 - down):
                return 0
            if Hs[u, i] >= c0 * (1 + up):
                return 1
        return None

    cap = {}
    members, nse_industry = ref["index"]
    for code, lbl in (("MICRO250", "U"), ("SML250", "S"), ("MID150", "M"), ("N100", "L")):
        for s in members.get(code, []):
            cap[s] = lbl
    cap_arr = np.array([cap.get(s, "") for s in syms], dtype=object)

    def stats_of(mask_t_i):
        tt, ii = np.nonzero(mask_t_i)
        if len(tt) == 0:
            return None
        out = {"n": int(len(tt))}
        for k in HORIZONS:
            f = FWD[k][tt, ii]
            f = f[np.isfinite(f)]
            out[f"n{k}"] = int(len(f))
            out[f"avg{k}"] = r(np.mean(f) * 100, 2) if len(f) else None
            out[f"med{k}"] = r(np.median(f) * 100, 2) if len(f) else None
            out[f"win{k}"] = r(np.mean(f > 0) * 100, 1) if len(f) else None
        c20 = complete20[tt]
        out["mfe"] = r(np.nanmean(MFE[tt[c20], ii[c20]]) * 100, 2) if c20.any() else None
        out["mae"] = r(np.nanmean(MAE[tt[c20], ii[c20]]) * 100, 2) if c20.any() else None
        return out

    base = LIQ & traded
    base[t_eod + 1:] = False
    baseline = stats_of(base)
    setup_stats = []
    for code, label, side, desc in SETUPS:
        m = SIG[code] & LIQ
        m[t_eod + 1:] = False
        st = stats_of(m) or {"n": 0}
        tt, ii = np.nonzero(m & complete20[:, None])
        hits = [hit_first(t, i) for t, i in zip(tt[-4000:], ii[-4000:])]
        hits = [h for h in hits if h is not None]
        st["hit"] = r(np.mean(hits) * 100, 1) if hits else None
        st["by_cap"] = {c: stats_of(m & (cap_arr == c)[None, :]) for c in ("L", "M", "S", "U", "")}
        st.update({"code": code, "label": label, "side": side, "desc": desc,
                   "today": int(SIG[code][t_eod].sum()), "today_liq": int((SIG[code][t_eod] & LIQ[t_eod]).sum()),
                   "live": int(SIG[code][T - 1].sum()) if is_live else None,
                   "needs_deliv": code in NEEDS_DELIV})
        setup_stats.append(st)

    # SMF score buckets x footprint bias
    buckets = [(0, 2), (2, 3), (3, 3.5), (3.5, 5), (5, 8), (8, 100)]
    smf_stats = []
    for lo, hi in buckets:
        for bname, bm in (("all", np.ones_like(BIAS, dtype=bool)), ("buy", BIAS >= 0.3), ("neutral", (BIAS > -0.3) & (BIAS < 0.3)), ("sell", BIAS <= -0.3)):
            m = base & (SMF >= lo) & (SMF < hi) & bm & (SRC == 1)
            st = stats_of(m)
            if st:
                st.update({"lo": lo, "hi": hi, "bias": bname})
                smf_stats.append(st)
    inflow_runs = []
    for runlen in (1, 2, 3, 4, 5):
        m = base & (SRC == 1)
        for k in range(runlen):
            m &= np.nan_to_num(shift(SMF, k)) >= 3.5
        if runlen < 5:
            m &= ~(np.nan_to_num(shift(SMF, runlen)) >= 3.5)
        st = stats_of(m)
        if st:
            st["run"] = runlen
            inflow_runs.append(st)

    # ---- per-stock rows ------------------------------------------------------------------------------
    tvd = tv.set_index("tv") if tv is not None else pd.DataFrame()
    names = ref["names"]
    t_last = T - 1
    te = t_eod

    RNG60 = div(roll(Hf, 60, "max", minp=40), roll(Lf, 60, "min", minp=40)) - 1

    def phase_at(t):
        """Simplified Wyckoff phase from trend, base width and the Institutional Score."""
        rng60 = RNG60[t]
        slope50 = div(SMA50[t], SMA50[max(0, t - 20)]) - 1
        slope200 = div(SMA200[t], SMA200[max(0, t - 20)]) - 1
        c, s50, s200, inst, h52 = Cf[t], SMA50[t], SMA200[t], INST[t], H52[t]
        out = np.full(N, "Transition", dtype=object)
        with np.errstate(invalid="ignore"):
            mup = (c > s50) & (s50 > s200) & (slope50 > 0.02) & (c >= 0.75 * h52)
            mdn = (c < s50) & (s50 < s200) & (slope50 < -0.02)
            acc = (rng60 <= 0.30) & (c <= s200 * 1.05) & ~(slope200 > 0.01) & (slope50 <= 0.02) & (inst >= 55)
            reacc = (rng60 <= 0.20) & (c > s200) & (slope200 > 0) & (inst >= 55) & ~mup
            dist = (c >= 0.85 * h52) & (np.abs(slope50) <= 0.03) & (rng60 <= 0.25) & (inst <= 45)
        out[mdn] = "Markdown"
        out[mup] = "Markup"
        out[dist] = "Distribution"
        out[reacc] = "Re-accumulation"
        out[acc] = "Accumulation"
        out[~np.isfinite(SMA50[t])] = ""
        return out

    phase = phase_at(te)
    phase20 = phase_at(max(0, te - 20))

    def streak_at(t):
        s = np.zeros(N, dtype=int)
        alive = np.ones(N, dtype=bool)
        for k in range(0, 30):
            if t - k < 0:
                break
            ok = np.nan_to_num(SMF[t - k]) >= 3.5
            alive &= ok
            s += alive
        return s

    streak = streak_at(te)
    n35 = np.nansum(np.nan_to_num(SMF[max(0, te - 19): te + 1]) >= 3.5, axis=0)
    nbuy20 = np.nansum((np.nan_to_num(SMF[max(0, te - 19): te + 1]) >= 3.5) & (np.nan_to_num(BIAS[max(0, te - 19): te + 1]) >= 0.3), axis=0)
    nsell20 = np.nansum((np.nan_to_num(SMF[max(0, te - 19): te + 1]) >= 3.5) & (np.nan_to_num(BIAS[max(0, te - 19): te + 1]) <= -0.3), axis=0)
    smf5 = np.nanmean(SMF[max(0, te - 4): te + 1], axis=0) if te >= 0 else np.full(N, np.nan)
    smfmax20 = np.nanmax(np.where(np.isfinite(SMF[max(0, te - 19): te + 1]), SMF[max(0, te - 19): te + 1], -1), axis=0)
    smfmax20 = np.where(smfmax20 < 0, np.nan, smfmax20)
    dn30 = np.nansum(DEALNET[max(0, te - 21): T], axis=0)
    dn90 = np.nansum(DEALNET[max(0, te - 63): T], axis=0)

    def bu_code(pr, oic):
        if not (np.isfinite(pr) and np.isfinite(oic)):
            return None
        if oic >= 0:
            return "LB" if pr >= 0 else "SB"
        return "SC" if pr >= 0 else "LU"

    def last_valid(arr, i, t):
        for u in range(t, max(-1, t - 3), -1):
            if np.isfinite(arr[u, i]):
                return arr[u, i]
        return np.nan

    stock_cols = ["sym", "name", "sector", "industry", "nse_ind", "cap", "fo", "sme", "series",
                  "close", "chg", "chg_eod", "r5", "r20", "mcap", "val20", "vr", "dp", "dp20", "dr",
                  "smf", "src", "smf1", "smf5", "smfmax20", "n35", "streak", "rising", "bias", "nbuy20", "nsell20",
                  "inst", "inst5", "inst20", "cd", "cf", "cm", "ct", "ci", "phase", "phase20", "rs", "a50", "a200", "off52",
                  "z_date", "z_lo", "z_hi", "z_vwap", "z_smf", "z_bias", "z_pos", "z_dist",
                  "oi", "oic1", "oic5", "oic20", "bu1", "bu5", "pcr", "dl30", "dl90",
                  "setups", "setups5", "lchg", "lrvol", "lsetups"]
    rows = []
    sector_of = {}
    for i, s in enumerate(syms):
        if not np.isfinite(Cf[t_last, i]) or nbars[te, i] < 20:
            continue
        t = tvd.loc[fnd.tv_key(s)] if fnd.tv_key(s) in tvd.index else None
        tg = (lambda k: None if t is None or k not in t.index or pd.isna(t[k]) or t[k] == "" else t[k])  # noqa: E731
        sector_of[s] = tg("sector")
        zl = zones_of.get(i) or []
        z = zl[-1] if zl else None
        zpos = zdist = None
        if z:
            c = Cf[t_last, i]
            zpos = "A" if c > z[2] else ("B" if c < z[1] else "I")
            zdist = (c / z[3] - 1) * 100 if z[3] else None
        sets = [k for k in SETUP_CODES if SIG[k][te, i]]
        sets5 = sorted({k for k in SETUP_CODES if SIG[k][max(0, te - 4): te + 1, i].any()})
        lsets = [k for k in SETUP_CODES if is_live and SIG[k][t_last, i]]
        mcap = Cf[t_last, i] * ISS[t_last, i] / 1e7 if np.isfinite(ISS[t_last, i]) else None
        is_fo = s in fo_syms
        pr1, pr5 = ret[te, i], div(Cf[te, i], Cf[max(0, te - 5), i]) - 1
        rows.append([
            s, eq_name.get(s) or names.get(s) or tg("tv_desc") or s, tg("sector"), tg("industry"), nse_industry.get(s),
            cap.get(s, ""), int(is_fo), int(bool(sme[i])), ser_last[i],
            r(Cf[t_last, i]), r(ret[t_last, i] * 100), r(pr1 * 100), r((div(Cf[t_last, i], Cf[max(0, t_last - 5), i]) - 1) * 100),
            r((div(Cf[t_last, i], Cf[max(0, t_last - 20), i]) - 1) * 100), r(mcap, 0), r(VAL20[te, i] / 1e7, 2),
            r(VR20[te, i]), r(DP[te, i], 1), r(np.nanmean(DP[max(0, te - 19): te + 1, i]) if np.isfinite(DP[max(0, te - 19): te + 1, i]).any() else None, 1),
            r(DR20[te, i]),
            r(SMF[te, i]), int(SRC[te, i]), r(SMF[te - 1, i]) if te > 0 else None, r(smf5[i]), r(smfmax20[i]),
            int(n35[i]), int(streak[i]),
            int(streak[i] >= 3 and np.nan_to_num(SMF[te, i]) > np.nan_to_num(SMF[te - 2, i])) if te >= 2 else 0,
            r(BIAS[te, i]), int(nbuy20[i]), int(nsell20[i]),
            r(INST[te, i], 0), r(INST[max(0, te - 5), i], 0), r(INST[max(0, te - 20), i], 0),
            r(Dc[te, i], 0), r(Fc[te, i], 0), r(Mc[te, i], 0), r(Tc[te, i], 0), r(Ic[te, i], 0),
            phase[i] or None, phase20[i] or None, r(RS[te, i], 0),
            int(Cf[t_last, i] > SMA50[te, i]) if np.isfinite(SMA50[te, i]) else None,
            int(Cf[t_last, i] > SMA200[te, i]) if np.isfinite(SMA200[te, i]) else None,
            r((div(Cf[t_last, i], H52[te, i]) - 1) * 100, 1),
            str(dates[z[0]].date()) if z else None, r(z[1]) if z else None, r(z[2]) if z else None, r(z[3]) if z else None,
            r(z[4]) if z else None, r(z[5]) if z else None, zpos, r(zdist, 1),
            r(last_valid(OI, i, te), 0) if is_fo else None,
            r(OIC[te, i] * 100, 1) if is_fo and np.isfinite(OIC[te, i]) else None,
            r(OIC5[te, i] * 100, 1) if is_fo and np.isfinite(OIC5[te, i]) else None,
            r(OIC20[te, i] * 100, 1) if is_fo and np.isfinite(OIC20[te, i]) else None,
            bu_code(pr1, OIC[te, i]) if is_fo else None, bu_code(pr5, OIC5[te, i]) if is_fo else None,
            r(last_valid(PCR, i, te), 2) if is_fo else None,
            r(dn30[i] / 1e7, 2) if dn30[i] else None, r(dn90[i] / 1e7, 2) if dn90[i] else None,
            ",".join(sets) or None, ",".join(sets5) or None,
            r(ret[t_last, i] * 100) if is_live else None, r(VR20[t_last, i]) if is_live else None,
            ",".join(lsets) or None,
        ])
    rows.sort(key=lambda x: -(x[stock_cols.index("mcap")] or 0))
    row_syms = [x[0] for x in rows]
    log(f"stock rows: {len(rows)}")

    # ---- SMF sheet: last 60 EOD sessions ---------------------------------------------------------
    s0 = max(0, te - SHEET_SESSIONS + 1)
    sheet = {
        "dates": [str(d.date()) for d in dates[s0: te + 1]],
        "syms": row_syms,
        "smf": [[r(SMF[t, col_of[s]]) for t in range(s0, te + 1)] for s in row_syms],
        "bias": [[(None if not np.isfinite(BIAS[t, col_of[s]]) else int(round(BIAS[t, col_of[s]] * 9)))
                  for t in range(s0, te + 1)] for s in row_syms],
    }

    # ---- recent signals with outcomes so far -----------------------------------------------------
    recent = []
    t_from = max(0, te - 40)
    for code in SETUP_CODES:
        tt, ii = np.nonzero(SIG[code][t_from:])
        for t, i in zip(tt + t_from, ii):
            if not np.isfinite(Cf[t, i]) or nbars[te, i] < 20:
                continue
            c0 = Cf[t, i]
            end = min(te, T - 1)
            if t < te:
                mx = np.nanmax(Hf[t + 1: end + 1, i]) / c0 - 1
                mn = np.nanmin(Lf[t + 1: end + 1, i]) / c0 - 1
            else:
                mx = mn = None
            recent.append([str(dates[t].date()), syms[i], code, r(c0), r(SMF[t, i]), r(BIAS[t, i]),
                           r((Cf[T - 1, i] / c0 - 1) * 100), r(mx * 100 if mx is not None else None),
                           r(mn * 100 if mn is not None else None), int(te - t) if t <= te else 0,
                           int(bool(LIQ[min(t, te), i])), int(t > te)])
    recent.sort(key=lambda x: (x[0], x[1]), reverse=True)
    recent = recent[:6000]

    # ---- market series ----------------------------------------------------------------------------
    m0 = max(0, te - MARKET_SESSIONS + 1)
    mk_dates = [str(d.date()) for d in dates[m0: te + 1]]
    liq_t = LIQ[m0: te + 1] & traded[m0: te + 1]
    S_ = SMF[m0: te + 1]
    B_ = BIAS[m0: te + 1]
    I_ = INST[m0: te + 1]
    with np.errstate(invalid="ignore"):
        market = {
            "dates": mk_dates,
            "n35": [int(x) for x in ((S_ >= 3.5) & liq_t).sum(axis=1)],
            "n8": [int(x) for x in ((S_ >= 8) & liq_t).sum(axis=1)],
            "nbuy": [int(x) for x in ((S_ >= 3.5) & (B_ >= 0.3) & liq_t).sum(axis=1)],
            "nsell": [int(x) for x in ((S_ >= 3.5) & (B_ <= -0.3) & liq_t).sum(axis=1)],
            "nliq": [int(x) for x in (liq_t & np.isfinite(S_)).sum(axis=1)],
            "med": [r(np.nanmedian(np.where(liq_t, S_, np.nan)[k]) if np.isfinite(np.where(liq_t, S_, np.nan)[k]).any() else None) for k in range(len(mk_dates))],
            "acc": [int(x) for x in ((I_ >= 60) & liq_t).sum(axis=1)],
            "dist": [int(x) for x in ((I_ <= 40) & liq_t).sum(axis=1)],
            "deliv": [r(np.nanmedian(np.where(liq_t, DP[m0: te + 1], np.nan)[k]), 1) if np.isfinite(np.where(liq_t, DP[m0: te + 1], np.nan)[k]).any() else None for k in range(len(mk_dates))],
        }
    ix = ni.IndexStore(data_dir).load(start)
    if len(ix):
        for code, key in (("NIFTY50", "nifty"), ("NIFTYSMALLCAP250", "small"), ("NIFTYMIDCAP150", "mid")):
            sub = ix[ix["code"] == code].drop_duplicates("date").set_index("date")["close"]
            market[key] = [r(x) for x in sub.reindex(pd.DatetimeIndex(mk_dates)).to_numpy()]
    if len(idx_fo):
        for u in ("NIFTY", "BANKNIFTY"):
            sub = idx_fo[idx_fo["symbol"] == u].drop_duplicates("date").set_index("date")
            pc = div(sub["pe_oi"].to_numpy(), sub["ce_oi"].to_numpy())
            market[f"pcr_{u.lower()}"] = [r(x) for x in pd.Series(pc, index=sub.index).reindex(pd.DatetimeIndex(mk_dates)).to_numpy()]

    part = sms.load_participant()
    participant = None
    if len(part):
        part = part[part["date"] >= mk_dates[0]] if mk_dates else part
        pdates = sorted(part["date"].unique())
        participant = {"dates": pdates, "types": {}}
        for typ in ("FII", "DII", "PRO", "CLIENT"):
            sub = part[part["type"].str.upper() == typ].set_index("date").reindex(pdates)
            participant["types"][typ] = {c: [r(x, 0) for x in sub[c].to_numpy()] for c in sm.PART_COLS[2:]}
    fii = sms.load_fiidii()
    fiidii = None
    if len(fii):
        fd = sorted(fii["date"].unique())[-250:]
        piv = fii.pivot_table(index="date", columns="cat", values="net").reindex(fd)
        fiidii = {"dates": fd, "fii": [r(x) for x in piv.get("FII", pd.Series(index=fd, dtype=float)).to_numpy()],
                  "dii": [r(x) for x in piv.get("DII", pd.Series(index=fd, dtype=float)).to_numpy()]}

    # ---- deals table (last 120 days) --------------------------------------------------------------
    deals_out = []
    if len(deal_rows):
        cut = dates[te] - pd.Timedelta(days=120)
        dsub = deal_rows[deal_rows["date"] >= cut].sort_values(["date", "val"], ascending=[False, False])
        last_px = {s: Cf[t_last, col_of[s]] for s in dsub["symbol"].unique() if s in col_of}
        for rec in dsub.itertuples(index=False):
            px = last_px.get(rec.symbol)
            deals_out.append([str(rec.date.date()), rec.kind[0].upper(), rec.symbol, str(rec.client)[:80], rec.side, r(rec.qty, 0),
                              r(rec.price), r(rec.val / 1e7, 2), rec.cls, int(bool(rec.intraday)),
                              r((px / rec.price - 1) * 100, 1) if px and rec.price else None])
        deals_out = deals_out[:6000]

    # ---- write files ---------------------------------------------------------------------------------
    site = Path(args.site)
    site.mkdir(parents=True, exist_ok=True)
    gen = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    meta = {
        "generated": gen, "nse_date": str(nse_date.date()), "latest": str(dates[-1].date()),
        "live": live if is_live else None, "live_vol_fraction": r(vfrac, 3) if is_live else None,
        "history_from": str(dates[0].date()), "deliv_sessions": deliv_days,
        "fo_sessions": int(fo["date"].nunique()) if len(fo) else 0,
        "deals_rows": int(len(deals)), "fiidii_rows": int(len(fii)) if len(fii) else 0,
        "smf_model": {"a": SMF_A, "b": SMF_B, "n": SMF_N, "r2": 0.945, "r2_fallback": 0.74, "calib_obs": 5366},
        "liq_value_cr": LIQ_VALUE / 1e7, "shards": SHARDS, "hist_sessions": HIST_SESSIONS,
        "classes": {c: l for c, l, _ in DEAL_CLASSES} | {"IND": "Individual / other"},
        "inst_classes": sorted(INST_CLASSES),
    }
    core = {
        "meta": meta,
        "setups": [{"code": c, "label": l, "side": sd, "desc": d} for c, l, sd, d in SETUPS],
        "stocks": {"cols": stock_cols, "rows": rows},
        "sheet": sheet,
        "stats": {"baseline": baseline, "setups": setup_stats, "smf": smf_stats, "runs": inflow_runs,
                  "from": str(dates[0].date()), "to": str(dates[te].date())},
        "recent": {"cols": ["date", "sym", "setup", "px", "smf", "bias", "ret", "mfe", "mae", "age", "liq", "live"], "rows": recent},
        "market": market, "participant": participant, "fiidii": fiidii,
        "deals": {"cols": ["date", "kind", "sym", "client", "side", "qty", "price", "val_cr", "cls", "intraday", "vs_now"], "rows": deals_out},
    }
    js = "window.SM_DATA=" + json.dumps(br.clean(core), separators=(",", ":"), allow_nan=False) + ";\n"
    (site / "smartmoney-data.js").write_text(js, encoding="utf-8")
    log(f"wrote smartmoney-data.js ({len(js) / 1e6:.2f} MB)")

    # roll helpers for the page's own live refresh (state after the last NSE session; a provisional row is left out)
    sl = slice(0, te + 1)
    lh = live_helpers(syms, dates[sl], C[sl], V[sl], ISS[sl], Cf[sl], Hf[sl], Lf[sl], traded[sl], nbars[sl], SMA50[sl],
                      SMA200[sl], H52[sl], LIQ[sl], {k: v[sl] for k, v in conds.items()}, zone_ev[sl], BIAS[sl],
                      {i: [z for z in zl if z[0] <= te] for i, zl in zones_of.items()}, trng[sl])
    lh["meta"]["generated"] = gen
    # exact closes behind the recent-signal prices (the data file rounds them), for "return since" with live prices
    lh["rc"] = {}
    for rw in recent:
        if rw[11]:
            continue
        t = dates.get_loc(pd.Timestamp(rw[0]))
        lh["rc"].setdefault(rw[0], {})[rw[1]] = float(Cf[t, col_of[rw[1]]])
    js = "window.SM_LIVE=" + json.dumps(br.clean(lh), separators=(",", ":"), allow_nan=False) + ";\n"
    (site / "smartmoney-live.js").write_text(js, encoding="utf-8")
    log(f"wrote smartmoney-live.js ({len(js) / 1e6:.2f} MB)")

    # per-stock history shards
    hdir = site / "sm-hist"
    hdir.mkdir(parents=True, exist_ok=True)
    h0 = max(0, T - HIST_SESSIONS)
    hdates = [str(d.date()) for d in dates[h0:]]
    shards = {k: {} for k in range(SHARDS)}
    for s in row_syms:
        i = col_of[s]
        sl = slice(h0, T)
        rec = {
            "o": [br.sig(x) for x in O[sl, i]], "h": [br.sig(x) for x in H[sl, i]], "l": [br.sig(x) for x in L[sl, i]],
            "c": [br.sig(x) for x in C[sl, i]], "v": [r(x, 0) for x in V[sl, i]], "dp": [r(x, 1) for x in DP[sl, i]],
            "ap": [br.sig(x) for x in AP[sl, i]], "smf": [r(x) for x in SMF[sl, i]],
            "b": [None if not np.isfinite(x) else int(round(x * 9)) for x in BIAS[sl, i]],
            "inst": [r(x, 0) for x in INST[sl, i]],
            "zones": [[str(dates[z[0]].date()), r(z[1]), r(z[2]), r(z[3]), r(z[4]), r(z[5])] for z in (zones_of.get(i) or [])],
            "sig": [[k, int(t - h0)] for k in SETUP_CODES for t in np.flatnonzero(SIG[k][sl, i]) + h0],
        }
        if s in fo_syms:
            rec["oi"] = [r(x, 0) for x in OI[sl, i]]
            rec["pcr"] = [r(x) for x in PCR[sl, i]]
        if len(deal_rows):
            dd = deal_rows[(deal_rows["symbol"] == s) & (deal_rows["date"] >= dates[h0])]
            rec["deals"] = [[str(x.date.date()), x.kind[0].upper(), str(x.client)[:60], x.side, r(x.qty, 0), r(x.price), x.cls, int(bool(x.intraday))]
                            for x in dd.itertuples(index=False)][-40:]
        shards[shard_of(s)][s] = rec
    for k, obj in shards.items():
        js = f"window.SM_HIST_CB&&SM_HIST_CB({k}," + json.dumps(br.clean({"dates": hdates, "s": obj}), separators=(",", ":"), allow_nan=False) + ");\n"
        (hdir / f"h{k:02d}.js").write_text(js, encoding="utf-8")
    log(f"wrote {SHARDS} history shard(s) in sm-hist/ ({sum((hdir / f'h{k:02d}.js').stat().st_size for k in range(SHARDS)) / 1e6:.1f} MB)")
    return 0


LIVE_SETUPS = [("RETEST", 10), ("POCKET", 3), ("SPRING", 10), ("UPTHRUST", 10), ("VCP", 10), ("EP", 10)]  # price/volume only


def live_helpers(syms, dates, C, V, ISS, Cf, Hf, Lf, traded, nbars, SMA50, SMA200, H52, LIQ, conds, zone_ev, BIAS, zones_of, trng):
    """State after the last NSE session (arrays end there) so smartmoney.html can roll the live session forward
    from TradingView quotes exactly as this script does: price columns, projected RVOL and the live setups."""
    T, N = Cf.shape
    e = T - 1

    def f(x):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    def prior(a, n, fn, minp=None):  # window of the n-1 rows before the next session
        blk = a[max(0, T - (n - 1)):]
        fin = np.isfinite(blk)
        cnt = fin.sum(axis=0)
        with np.errstate(invalid="ignore"):
            if fn == "sum":
                return np.where(fin, blk, 0).sum(axis=0), cnt
            if fn == "max":
                return np.where(fin, blk, -np.inf).max(axis=0), cnt
            return np.where(fin, blk, np.inf).min(axis=0), cnt

    H = {}
    put = lambda k, arr: H.__setitem__(k, [f(x) for x in arr])  # noqa: E731
    put("c", Cf[e]); put("v", np.where(np.isfinite(C[e]), V[e], np.nan)); put("iss", ISS[e])
    put("a5", Cf[e - 4] if e >= 4 else np.full(N, np.nan)); put("a20", Cf[e - 19] if e >= 19 else np.full(N, np.nan))
    put("sma50", SMA50[e]); put("sma200", SMA200[e]); put("h52", H52[e])
    blk = V[max(0, T - 20):]
    cnt = np.isfinite(blk).sum(axis=0)
    with np.errstate(invalid="ignore"):
        put("v20", np.where(cnt >= 10, np.nansum(blk, axis=0) / np.maximum(cnt, 1), np.nan))
    ret = div(Cf, shift(Cf, 1)) - 1
    dn_vol = np.nan_to_num(np.where(ret < 0, V, 0))
    put("mdn10", np.where(min(10, T) >= 5, dn_vol[max(0, T - 10):].max(axis=0), np.nan))
    s, n = prior(Cf, 10, "sum"); put("s9", np.where(n == 9, s, np.nan))
    for nn, key in ((50, "49"), (200, "199")):
        s, n = prior(Cf, nn, "sum"); put("s" + key, s); H["n" + key] = n.astype(int).tolist()
    m, n = prior(Hf, 250, "max"); put("h249", m); H["nh249"] = n.astype(int).tolist()
    blk = Lf[max(0, T - 40):]
    put("l40", np.where(np.isfinite(blk).sum(axis=0) >= 30, np.where(np.isfinite(blk), blk, np.inf).min(axis=0), np.nan))
    blk = Hf[max(0, T - 40):]
    put("h40", np.where(np.isfinite(blk).sum(axis=0) >= 30, np.where(np.isfinite(blk), blk, -np.inf).max(axis=0), np.nan))
    s, n = prior(trng, 10, "sum"); put("t9", np.where(n == 9, s, np.nan))
    s, n = prior(trng, 50, "sum"); put("t49", s); H["nt49"] = n.astype(int).tolist()
    for nn, key in ((10, "9"), (50, "49")):
        s, n = prior(V, nn, "sum"); put("v" + key, s); H["nv" + key] = n.astype(int).tolist()
    pc = np.zeros(N, dtype=int)
    for bit, (code, cool) in enumerate(LIVE_SETUPS):
        c = np.nan_to_num(conds[code][max(0, T - cool):].astype(float)) > 0
        pc |= (c.any(axis=0).astype(int) << bit)
    H["pc"] = pc.tolist()
    H["liq"] = [int(x) for x in LIQ[e]]
    H["nb"] = nbars[e].astype(int).tolist()
    fin = np.isfinite(C)
    last_t = np.where(fin.any(axis=0), T - 1 - np.argmax(fin[::-1], axis=0), -1)
    H["lt"] = [str(pd.Timestamp(dates[t]).date()) if t >= 0 else None for t in last_t]   # last NSE session traded
    # buy-side SMF zones whose retest watch (60 sessions after the zone day) is still open at the next session
    armed = []
    for i in range(N):
        out = []
        for t in np.flatnonzero(zone_ev[:, i]):
            if t + 60 < T or not (BIAS[t, i] >= 0.3):
                continue
            lo, hi = Lf[t, i], Hf[t, i]
            ran, done = False, False
            for u in range(t + 1, T):
                if Hf[u, i] >= hi * 1.05:
                    ran = True
                elif ran and Lf[u, i] <= hi and Cf[u, i] >= lo:
                    done = True
                    break
                if Cf[u, i] < lo:
                    done = True
                    break
            if not done:
                out.append([f(lo), f(hi), int(ran)])
        armed.append(out or None)
    H["zr"] = armed
    last = [zones_of.get(i)[-1] if zones_of.get(i) else None for i in range(N)]
    H["zlo"] = [f(z[1]) if z else None for z in last]; H["zhi"] = [f(z[2]) if z else None for z in last]
    H["zvw"] = [f(z[3]) if z else None for z in last]
    nxt = bd.next_weekday(pd.Timestamp(dates[-1]))
    meta = {"eod": str(pd.Timestamp(dates[-1]).date()), "next": str(nxt.date()), "setups": [c for c, _ in LIVE_SETUPS],
            "vol_curve": VOL_CURVE}
    return {"meta": meta, "syms": list(syms), "h": H}


def shard_of(sym: str) -> int:
    """Stable shard number; the page uses the same CRC32 to find a stock's file."""
    return zlib.crc32(sym.encode("utf-8")) % SHARDS


if __name__ == "__main__":
    sys.exit(main())
