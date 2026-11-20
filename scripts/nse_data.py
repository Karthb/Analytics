"""
Official NSE end-of-day data: download, normalise and store.

Sources (all public files on nsearchives.nseindia.com, no login, no cookies):
  * UDiFF CM bhavcopy  -> daily OHLC, previous close, volume, value, trades, ISIN
  * PR bundle (MCAP)   -> issue size (shares) per security  -> market cap + split/bonus detection
  * PR bundle (Bc)     -> corporate-action announcements (bonus / split text, for reference)
  * sec_bhavdata_full  -> delivery % (only fetched for recent days)
  * EQUITY_L.csv       -> list of NSE equities, company names, face value, listing date
  * symbolchange.csv   -> old -> new symbol mapping (keeps history continuous after renames)
  * index constituent CSVs + F&O lot file -> Nifty index membership / F&O flag

Each trading day is stored once as data/eod/YYYY/YYYYMMDD.csv.gz (unadjusted, as published).
Split/bonus adjustment is done later in build_data.py so the raw store never needs rewriting.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

NSE_ARCHIVES = os.environ.get("NSE_ARCHIVES", "https://nsearchives.nseindia.com").rstrip("/")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
EQUITY_SERIES = ("EQ", "BE", "BZ", "SM", "ST")
SERIES_PRIORITY = {s: i for i, s in enumerate(EQUITY_SERIES)}
STORE_COLUMNS = [
    "symbol", "isin", "series", "open", "high", "low", "close", "prev_close",
    "volume", "value", "trades", "issue_size", "deliv_pct",
]
POLITE_DELAY = float(os.environ.get("NSE_DELAY", "0.25"))  # seconds between requests per worker

INDEX_FILES = {
    "N50": "ind_nifty50list.csv",
    "NN50": "ind_niftynext50list.csv",
    "N100": "ind_nifty100list.csv",
    "N200": "ind_nifty200list.csv",
    "N500": "ind_nifty500list.csv",
    "MID150": "ind_niftymidcap150list.csv",
    "SML250": "ind_niftysmallcap250list.csv",
    "MICRO250": "ind_niftymicrocap250_list.csv",
}


def log(msg: str) -> None:
    print(f"[nse] {msg}", flush=True)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=4, connect=4, read=4, backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD", "POST"]),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def http_get(session: requests.Session, url: str, timeout: int = 45) -> bytes | None:
    """GET a file. Returns None for 404 (file not published / no trading session)."""
    time.sleep(POLITE_DELAY)
    r = session.get(url, timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()  # 403 etc. raise, so a block is never mistaken for "no trading day"
    body = r.content
    # NSE occasionally answers with an HTML error page and status 200
    if body[:15].lstrip().lower().startswith((b"<!doctype", b"<html")):
        return None
    return body


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "").str.strip(), errors="coerce")


def _zip_members(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {Path(n).name: zf.read(n) for n in zf.namelist() if not n.endswith("/")}


def _read_csv(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False, skipinitialspace=True)
    df.columns = [str(c).strip() for c in df.columns]
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip()
    return df


# --------------------------------------------------------------------------- daily files
def udiff_url(d: dt.date) -> str:
    return f"{NSE_ARCHIVES}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"


def pr_url(d: dt.date) -> str:
    return f"{NSE_ARCHIVES}/archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip"


def secbhav_url(d: dt.date) -> str:
    return f"{NSE_ARCHIVES}/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"


def parse_udiff(content: bytes, expect: dt.date | None = None) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    members = _zip_members(content)
    raw = next(iter(members.values()))
    df = _read_csv(raw)
    if expect is not None and len(df) and df["TradDt"].iloc[0] != expect.isoformat():
        return None  # file does not belong to the requested date
    df = df[(df["FinInstrmTp"] == "STK") & (df["SctySrs"].isin(EQUITY_SERIES))]
    out = pd.DataFrame({
        "symbol": df["TckrSymb"].values,
        "isin": df["ISIN"].values,
        "series": df["SctySrs"].values,
        "open": _num(df["OpnPric"]).values,
        "high": _num(df["HghPric"]).values,
        "low": _num(df["LwPric"]).values,
        "close": _num(df["ClsPric"]).values,
        "prev_close": _num(df["PrvsClsgPric"]).values,
        "volume": _num(df["TtlTradgVol"]).values,
        "value": _num(df["TtlTrfVal"]).values,
        "trades": _num(df["TtlNbOfTxsExctd"]).values,
    })
    names = pd.DataFrame({"symbol": df["TckrSymb"].values, "name": df["FinInstrmNm"].values})
    return out, names


def parse_secbhav(content: bytes, expect: dt.date | None = None) -> pd.DataFrame | None:
    df = _read_csv(content)
    if expect is not None and len(df):
        d1 = pd.to_datetime(df["DATE1"].iloc[0], format="%d-%b-%Y", errors="coerce")
        if pd.isna(d1) or d1.date() != expect:
            # On Sundays/holidays NSE re-serves the PREVIOUS session's file under the new date.
            return None
    df = df[df["SERIES"].isin(EQUITY_SERIES)]
    return pd.DataFrame({
        "symbol": df["SYMBOL"].values,
        "isin": "",
        "series": df["SERIES"].values,
        "open": _num(df["OPEN_PRICE"]).values,
        "high": _num(df["HIGH_PRICE"]).values,
        "low": _num(df["LOW_PRICE"]).values,
        "close": _num(df["CLOSE_PRICE"]).values,
        "prev_close": _num(df["PREV_CLOSE"]).values,
        "volume": _num(df["TTL_TRD_QNTY"]).values,
        "value": (_num(df["TURNOVER_LACS"]) * 1e5).values,
        "trades": _num(df["NO_OF_TRADES"]).values,
        "deliv_pct": _num(df["DELIV_PER"]).values,
    })


def parse_pr(content: bytes) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Returns (mcap issue sizes, corporate-action announcements) from the PR bundle."""
    members = _zip_members(content)
    mcap = bc = None
    for name, raw in members.items():
        low = name.lower()
        if low.startswith("mcap"):
            df = _read_csv(raw)
            cols = {c.lower(): c for c in df.columns}
            sym, ser = cols.get("symbol"), cols.get("series")
            issue = next((cols[c] for c in cols if c.startswith("issue size")), None)
            if sym and ser and issue:
                mcap = pd.DataFrame({
                    "symbol": df[sym].values, "series": df[ser].values,
                    "issue_size": _num(df[issue]).values,
                })
        elif low.startswith("bc"):
            df = _read_csv(raw)
            if {"SYMBOL", "SERIES", "EX_DT", "PURPOSE"} <= set(df.columns):
                df = df[df["SERIES"].isin(EQUITY_SERIES)]
                bc = pd.DataFrame({
                    "symbol": df["SYMBOL"].values,
                    "ex_date": df["EX_DT"].values,
                    "purpose": df["PURPOSE"].values,
                })
    return mcap, bc


def fetch_day(session: requests.Session, d: dt.date, with_delivery: bool):
    """Download one trading day. Returns (frame, names, corp_actions) or None if no session."""
    names = None
    content = http_get(session, udiff_url(d))
    parsed = parse_udiff(content, d) if content is not None else None
    if parsed is not None:
        day, names = parsed
        day["deliv_pct"] = float("nan")
    else:
        sec = http_get(session, secbhav_url(d))
        day = parse_secbhav(sec, d) if sec is not None else None
        if day is None:
            return None  # no trading session on this date
        with_delivery = False  # already have it

    day["issue_size"] = float("nan")
    bc = None
    pr = http_get(session, pr_url(d))
    if pr is not None:
        try:
            mcap, bc = parse_pr(pr)
            if mcap is not None:
                m = mcap.drop_duplicates(["symbol", "series"]).set_index(["symbol", "series"])["issue_size"]
                day["issue_size"] = pd.MultiIndex.from_frame(day[["symbol", "series"]]).map(m).values
        except Exception as exc:  # PR bundle is optional
            log(f"{d}: PR bundle unreadable ({exc})")

    if with_delivery:
        sec = http_get(session, secbhav_url(d))
        if sec is not None:
            try:
                sb = parse_secbhav(sec, d)
                if sb is not None:
                    s = sb.drop_duplicates(["symbol", "series"]).set_index(["symbol", "series"])["deliv_pct"]
                    day["deliv_pct"] = pd.MultiIndex.from_frame(day[["symbol", "series"]]).map(s).values
            except Exception as exc:
                log(f"{d}: delivery file unreadable ({exc})")

    return day[STORE_COLUMNS], names, bc


# --------------------------------------------------------------------------- store
class EodStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.eod = self.root / "eod"
        self.meta = self.root / "meta"
        self.eod.mkdir(parents=True, exist_ok=True)
        self.meta.mkdir(parents=True, exist_ok=True)
        self.no_session_file = self.eod / "no_session_days.txt"

    def path_for(self, d: dt.date) -> Path:
        return self.eod / f"{d:%Y}" / f"{d:%Y%m%d}.csv.gz"

    def existing_dates(self) -> set[dt.date]:
        out = set()
        for p in self.eod.glob("*/*.csv.gz"):
            try:
                out.add(dt.datetime.strptime(p.name[:8], "%Y%m%d").date())
            except ValueError:
                pass
        return out

    def no_session_dates(self) -> set[dt.date]:
        if not self.no_session_file.exists():
            return set()
        return {dt.date.fromisoformat(x.strip()) for x in self.no_session_file.read_text().split() if x.strip()}

    def save_no_session(self, dates: set[dt.date]) -> None:
        self.no_session_file.write_text("\n".join(sorted(d.isoformat() for d in dates)) + "\n")

    def write_day(self, d: dt.date, frame: pd.DataFrame) -> None:
        p = self.path_for(d)
        p.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(p, index=False, compression={"method": "gzip", "mtime": 0}, float_format="%.10g")

    def update_names(self, names: pd.DataFrame) -> None:
        p = self.meta / "names.csv"
        old = pd.read_csv(p, dtype=str, keep_default_na=False) if p.exists() else pd.DataFrame(columns=["symbol", "name"])
        new = pd.concat([old, names]).drop_duplicates("symbol", keep="last").sort_values("symbol")
        new.to_csv(p, index=False)

    def update_corp_actions(self, bc: pd.DataFrame) -> None:
        p = self.meta / "corp_actions.csv"
        old = pd.read_csv(p, dtype=str, keep_default_na=False) if p.exists() else pd.DataFrame(columns=["symbol", "ex_date", "purpose"])
        new = pd.concat([old, bc.astype(str)]).drop_duplicates().sort_values(["symbol", "ex_date"])
        new.to_csv(p, index=False)

    def prune(self, keep_from: dt.date) -> int:
        n = 0
        for p in self.eod.glob("*/*.csv.gz"):
            try:
                if dt.datetime.strptime(p.name[:8], "%Y%m%d").date() < keep_from:
                    p.unlink()
                    n += 1
            except ValueError:
                pass
        return n

    def load(self, start: dt.date) -> pd.DataFrame:
        frames = []
        for p in sorted(self.eod.glob("*/*.csv.gz")):
            d = dt.datetime.strptime(p.name[:8], "%Y%m%d").date()
            if d < start:
                continue
            f = pd.read_csv(p, dtype={"symbol": str, "isin": str, "series": str}, keep_default_na=False,
                            na_values={c: [""] for c in STORE_COLUMNS[3:]})
            f["date"] = pd.Timestamp(d)
            frames.append(f)
        if not frames:
            return pd.DataFrame(columns=STORE_COLUMNS + ["date"])
        df = pd.concat(frames, ignore_index=True)
        for c in STORE_COLUMNS[3:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["isin"] = df["isin"].fillna("").astype(str)
        return df


def remove_duplicate_days(store: EodStore, df: pd.DataFrame) -> pd.DataFrame:
    """Delete stored days that are copies of the previous session (written by older versions
    when NSE re-served the last bhavcopy on a Sunday/holiday). Returns df without them."""
    if df.empty:
        return df
    dates = sorted(df["date"].unique())
    by_date = {d: g.set_index("symbol")[["close", "volume"]] for d, g in df.groupby("date")}
    bad = []
    prev = by_date[dates[0]]
    for d in dates[1:]:
        cur = by_date[d]
        j = cur.join(prev, rsuffix="_p", how="inner")
        if len(j) and ((j["close"] == j["close_p"]) & (j["volume"] == j["volume_p"])).mean() > 0.5:
            bad.append(pd.Timestamp(d))
            continue  # keep comparing with the real previous session
        prev = cur
    if not bad:
        return df
    ns = store.no_session_dates()
    for d in bad:
        p = store.path_for(d.date())
        if p.exists():
            p.unlink()
        ns.add(d.date())
    store.save_no_session(ns)
    log(f"removed {len(bad)} duplicate non-trading day(s): {', '.join(str(d.date()) for d in bad[:5])}{' ...' if len(bad) > 5 else ''}")
    return df[~df["date"].isin(bad)].reset_index(drop=True)


def update_store(store: EodStore, start: dt.date, today: dt.date, session: requests.Session,
                 workers: int = 4) -> list[dt.date]:
    """Download every missing trading day between start and today. Returns new dates."""
    have = store.existing_dates()
    no_session = store.no_session_dates()
    todo = []
    d = start
    while d <= today:
        if d not in have and d not in no_session:
            todo.append(d)
        d += dt.timedelta(days=1)
    if not todo:
        log("store up to date")
        return []
    log(f"checking {len(todo)} day(s): {todo[0]} .. {todo[-1]}")

    def job(day: dt.date):
        try:
            return day, fetch_day(session, day, with_delivery=(today - day).days <= 10), None
        except Exception as exc:
            return day, None, exc

    added, errors = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for day, result, err in ex.map(job, todo):
            if err is not None:
                errors += 1
                log(f"{day}: download error ({err}) - will retry next run")
                continue
            if result is None:
                # Only mark as 'no session' once NSE has certainly had time to publish.
                if (today - day).days >= 4:
                    no_session.add(day)
                continue
            frame, names, bc = result
            if frame.empty:
                continue
            store.write_day(day, frame)
            if names is not None:
                store.update_names(names)
            if bc is not None and not bc.empty:
                store.update_corp_actions(bc)
            added.append(day)
    store.save_no_session(no_session)
    log(f"added {len(added)} trading day(s); {errors} error(s)")
    return sorted(added)


# --------------------------------------------------------------------------- reference files
def fetch_equity_list(session: requests.Session) -> pd.DataFrame | None:
    raw = http_get(session, f"{NSE_ARCHIVES}/content/equities/EQUITY_L.csv")
    if raw is None:
        return None
    df = _read_csv(raw)
    cols = {c.upper().strip(): c for c in df.columns}
    return pd.DataFrame({
        "symbol": df[cols["SYMBOL"]],
        "company": df[cols["NAME OF COMPANY"]],
        "listed": df[cols.get("DATE OF LISTING", cols["SYMBOL"])],
        "isin": df[cols.get("ISIN NUMBER", cols["SYMBOL"])],
        "fv": _num(df[cols["FACE VALUE"]]) if "FACE VALUE" in cols else float("nan"),
    })


def fetch_symbol_changes(session: requests.Session) -> pd.DataFrame | None:
    raw = http_get(session, f"{NSE_ARCHIVES}/content/equities/symbolchange.csv")
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    rows = []
    for line in text.splitlines():
        parts = [p.strip().strip('"') for p in line.rsplit(",", 3)]  # company names may contain commas
        if len(parts) == 4 and parts[1] and parts[2] and parts[1].upper() != "OLD SYMBOL":
            rows.append(parts)
    df = pd.DataFrame(rows, columns=["company", "old", "new", "date"])
    df["date"] = pd.to_datetime(df["date"], format="%d-%b-%Y", errors="coerce")
    return df.dropna(subset=["date"])


def fetch_index_members(session: requests.Session) -> tuple[dict[str, set[str]], dict[str, str]]:
    members: dict[str, set[str]] = {}
    industry: dict[str, str] = {}
    for code, fname in INDEX_FILES.items():
        try:
            raw = http_get(session, f"{NSE_ARCHIVES}/content/indices/{fname}")
            if raw is None:
                log(f"index file missing: {fname}")
                continue
            df = _read_csv(raw)
            members[code] = set(df["Symbol"])
            if "Industry" in df.columns:
                industry.update(dict(zip(df["Symbol"], df["Industry"])))
        except Exception as exc:
            log(f"index file {fname} failed: {exc}")
    return members, industry


def fetch_fo_symbols(session: requests.Session) -> set[str]:
    try:
        raw = http_get(session, f"{NSE_ARCHIVES}/content/fo/fo_mktlots.csv")
        if raw is None:
            return set()
        df = _read_csv(raw)
        col = next(c for c in df.columns if c.strip().upper() == "SYMBOL")
        syms = {s.strip() for s in df[col] if s.strip() and s.strip().upper() != "SYMBOL"}
        return {s for s in syms if not s.upper().startswith(("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"))}
    except Exception as exc:
        log(f"F&O list failed: {exc}")
        return set()
