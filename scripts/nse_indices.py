"""
NSE index data for the Sector Rotation page: daily OHLC of every NSE index + index constituents.

Sources (public files on nsearchives.nseindia.com, the same host the screener already uses):
  * ind_close_all_DDMMYYYY.csv  -> one file per session with O/H/L/C, P/E, P/B, yield of ~190 indices
  * ind_<index>list.csv         -> constituents of sectoral / thematic / broad indices

Each session is stored once as data/indices/YYYY/YYYYMMDD.csv.gz (as published, names untouched).
Constituent lists are cached in data/meta/sector_members.json and refreshed at most once a day.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

import nse_data as nse

INDEX_COLUMNS = ["name", "open", "high", "low", "close", "volume", "turnover_cr", "pe", "pb", "dy"]

# index code -> constituent file name(s) on nsearchives /content/indices/ (first that works wins).
# Verified 21 Nov 2026. Extra spellings are tried for indices whose file name was not found yet.
MEMBER_FILES: dict[str, list[str]] = {
    "NIFTY50": ["ind_nifty50list.csv"],
    "NIFTYNEXT50": ["ind_niftynext50list.csv"],
    "NIFTY100": ["ind_nifty100list.csv"],
    "NIFTY200": ["ind_nifty200list.csv"],
    "NIFTY500": ["ind_nifty500list.csv"],
    "NIFTYMIDCAP50": ["ind_niftymidcap50list.csv"],
    "NIFTYMIDCAP100": ["ind_niftymidcap100list.csv"],
    "NIFTYMIDCAP150": ["ind_niftymidcap150list.csv"],
    "NIFTYMIDCAPSELECT": ["ind_niftymidcapselect_list.csv"],
    "NIFTYSMALLCAP50": ["ind_niftysmallcap50list.csv"],
    "NIFTYSMALLCAP100": ["ind_niftysmallcap100list.csv"],
    "NIFTYSMALLCAP250": ["ind_niftysmallcap250list.csv"],
    "NIFTYMICROCAP250": ["ind_niftymicrocap250_list.csv"],
    "NIFTYLARGEMIDCAP250": ["ind_niftylargemidcap250list.csv"],
    "NIFTYMIDSMALLCAP400": ["ind_niftymidsmallcap400list.csv"],
    "NIFTYTOTALMARKET": ["ind_niftytotalmarket_list.csv"],
    # sectoral
    "NIFTYAUTO": ["ind_niftyautolist.csv"],
    "NIFTYBANK": ["ind_niftybanklist.csv"],
    "NIFTYCONSUMERDURABLES": ["ind_niftyconsumerdurableslist.csv"],
    "NIFTYFINANCIALSERVICES": ["ind_niftyfinancelist.csv"],
    "NIFTYFINANCIALSERVICES2550": ["ind_niftyfinancialservices25_50list.csv"],
    "NIFTYFINANCIALSERVICESEXBANK": ["ind_niftyfinancialservicesexbank_list.csv"],
    "NIFTYFMCG": ["ind_niftyfmcglist.csv"],
    "NIFTYHEALTHCARE": ["ind_niftyhealthcarelist.csv"],
    "NIFTYIT": ["ind_niftyitlist.csv"],
    "NIFTYMEDIA": ["ind_niftymedialist.csv"],
    "NIFTYMETAL": ["ind_niftymetallist.csv"],
    "NIFTYOILGAS": ["ind_niftyoilgaslist.csv"],
    "NIFTYPHARMA": ["ind_niftypharmalist.csv"],
    "NIFTYPRIVATEBANK": ["ind_nifty_privatebanklist.csv", "ind_niftyprivatebanklist.csv"],
    "NIFTYPSUBANK": ["ind_niftypsubanklist.csv"],
    "NIFTYREALTY": ["ind_niftyrealtylist.csv"],
    "NIFTYMIDSMALLHEALTHCARE": ["ind_niftymidsmallhealthcare_list.csv"],
    "NIFTYMIDSMALLINDIACONSUMPTION": ["ind_niftymidsmallindiaconsumption_list.csv"],
    "NIFTYMIDSMALLFINANCIALSERVICES": ["ind_niftymidsmallfinancialservices_list.csv",
                                       "ind_niftymidsmallfinancialserviceslist.csv"],
    "NIFTYMIDSMALLITTELECOM": ["ind_niftymidsmallittelecom_list.csv", "ind_niftymidsmallitandtelecom_list.csv"],
    "NIFTYCHEMICALS": ["ind_niftychemicals_list.csv", "ind_niftychemicalslist.csv", "ind_nifty_chemicals_list.csv"],
    "NIFTYCAPITALGOODS": ["ind_niftycapitalgoods_list.csv", "ind_niftycapitalgoodslist.csv"],
    "NIFTYCEMENT": ["ind_niftycement_list.csv", "ind_niftycementlist.csv"],
    # thematic
    "NIFTYCOMMODITIES": ["ind_niftycommoditieslist.csv"],
    "NIFTYINDIACONSUMPTION": ["ind_niftyconsumptionlist.csv"],
    "NIFTYCOREHOUSING": ["ind_niftycorehousing_list.csv"],
    "NIFTYCPSE": ["ind_niftycpselist.csv"],
    "NIFTYENERGY": ["ind_niftyenergylist.csv"],
    "NIFTYINDIADEFENCE": ["ind_niftyindiadefence_list.csv"],
    "NIFTYINDIADIGITAL": ["ind_niftyindiadigital_list.csv"],
    "NIFTYINDIAMANUFACTURING": ["ind_niftyindiamanufacturing_list.csv"],
    "NIFTYINDIATOURISM": ["ind_niftyindiatourism_list.csv"],
    "NIFTYINFRASTRUCTURE": ["ind_niftyinfralist.csv"],
    "NIFTYMNC": ["ind_niftymnclist.csv"],
    "NIFTYMOBILITY": ["ind_niftymobility_list.csv"],
    "NIFTYPSE": ["ind_niftypselist.csv"],
    "NIFTYSERVICESSECTOR": ["ind_niftyservicelist.csv"],
    "NIFTYTRANSPORTATIONLOGISTICS": ["ind_niftytransportationandlogistics_list.csv"],
    "NIFTYCAPITALMARKETS": ["ind_niftycapitalmarkets_list.csv", "ind_niftycapitalmarketslist.csv"],
    "NIFTYHOUSING": ["ind_niftyhousing_list.csv", "ind_niftyhousinglist.csv"],
    "NIFTYEVNEWAGEAUTOMOTIVE": ["ind_niftyevnewageautomotive_list.csv", "ind_niftyev_newageautomotive_list.csv"],
    "NIFTYRURAL": ["ind_niftyrural_list.csv", "ind_niftyrurallist.csv"],
}

# When NSE has no constituent file for a sectoral index, approximate it with Nifty 500 members of the
# matching NSE industry (shown as "approx." in the page).
INDUSTRY_FALLBACK = {
    "NIFTYCHEMICALS": "Chemicals",
    "NIFTYCAPITALGOODS": "Capital Goods",
    "NIFTYCEMENT": "Construction Materials",
    "NIFTYCONSTRUCTION": "Construction",
    "NIFTYTELECOMMUNICATIONS": "Telecommunication",
    "NIFTYCONSUMERSERVICES": "Consumer Services",
    "NIFTYPOWER": "Power",
    "NIFTYTEXTILES": "Textiles",
}

# TradingView tickers for the indices its screener feed carries (live values during market hours)
TV_INDEX = {
    "NIFTY50": "NIFTY", "NIFTYBANK": "BANKNIFTY", "NIFTYIT": "CNXIT", "NIFTY500": "CNX500",
    "NIFTYENERGY": "CNXENERGY", "NIFTYFINANCIALSERVICES": "CNXFINANCE", "NIFTYNEXT50": "NIFTYJR",
    "NIFTYSMALLCAP100": "CNXSMALLCAP", "NIFTYMIDCAP100": "CNXMIDCAP", "NIFTYMIDCAPSELECT": "NIFTY_MID_SELECT",
    "INDIAVIX": "INDIAVIX",
}

# "Core sectors" preset used as the default RRG universe
CORE_SECTORS = [
    "NIFTYAUTO", "NIFTYBANK", "NIFTYCHEMICALS", "NIFTYCONSUMERDURABLES", "NIFTYENERGY",
    "NIFTYFINANCIALSERVICES", "NIFTYFMCG", "NIFTYHEALTHCARE", "NIFTYIT", "NIFTYMEDIA", "NIFTYMETAL",
    "NIFTYOILGAS", "NIFTYPHARMA", "NIFTYPRIVATEBANK", "NIFTYPSUBANK", "NIFTYREALTY",
]

SECTORAL_WORDS = (
    "AUTO", "BANK", "CEMENT", "CHEMICALS", "CAPITAL GOODS", "COMMERCIAL & TRANSPORT", "CONSTRUCTION",
    "CONSUMER DURABLES", "CONSUMER SERVICES", "FINANCIAL SERVICES", "FMCG", "HEALTHCARE", "HOSPITALS",
    "HOUSING FINANCE", "INSURANCE", " IT", "MEDIA", "METAL", "NBFC", "OIL & GAS", "PHARMA", "POWER",
    "PRIVATE BANK", "PSU BANK", "REALTY", "RETAIL", "TELECOMMUNICATIONS", "SMALL FINANCE BANKS",
)
STRATEGY_WORDS = (
    "ALPHA", "BETA", "LOW VOLATILITY", "LOW-VOLATILITY", "QUALITY", "MOMENTUM", "VALUE", "EQUAL WEIGHT",
    "EQUAL-CAP", "DIVIDEND", "ESG", "SHARIAH", "GROWTH", "LIQUID 15", "MULTIFACTOR", "FLEXICAP", "AHIMSA",
    "TOP 10", "TOP 15", "TOP 20", "FPI 150",
)
BROAD_RE = re.compile(
    r"^NIFTY (50|NEXT 50|100|200|500|1000|NEXT 100|TOTAL MARKET|MIDCAP (50|100|150|SELECT)|SMALLCAP (50|100|250|500)|"
    r"MICROCAP 250|LARGEMIDCAP 250|MIDSMALLCAP 400|MIDSMALLCAP400 50:50|SME EMERGE)$|^NIFTY ?500 MULTICAP 50:25:25$")
EXCLUDE_RE = re.compile(
    r"G-SEC|BOND|1D RATE|DIVIDEND POINTS|INVERSE|LEVERAGE|FUTURES|ARBITRAGE|\bTR\b|USD|REIT|SDL|T-BILL|CORPORATE BOND",
    re.I)


def log(msg: str) -> None:
    print(f"[indices] {msg}", flush=True)


def index_code(name: str) -> str:
    n = re.sub(r"\s+INDEX$", "", str(name).strip().upper())
    return re.sub(r"[^A-Z0-9]", "", n)


def index_label(name: str) -> str:
    """Short label for charts: 'Nifty Oil & Gas' -> 'Oil & Gas', 'Nifty 50' -> 'Nifty 50'."""
    n = re.sub(r"\s+Index$", "", str(name).strip(), flags=re.I)
    short = re.sub(r"^nifty\s+", "", n, flags=re.I)
    short = re.sub(r"^India Corporate Group Index - ", "", short, flags=re.I)
    if re.match(r"^(\d|next|midcap|smallcap|microcap|largemidcap|midsmallcap|total|500|100|200|1000)", short, re.I):
        return "Nifty " + short
    return short


def index_category(name: str) -> str | None:
    """broad / sector / thematic / strategy, or None for non-equity indices we skip."""
    n = re.sub(r"\s+INDEX$", "", str(name).strip().upper())
    if n == "INDIA VIX":
        return "vix"
    if EXCLUDE_RE.search(n):
        return None
    if BROAD_RE.match(n):
        return "broad"
    if any(w in n for w in STRATEGY_WORDS):
        return "strategy"
    if n.startswith("NIFTY MIDSMALL ") or n.startswith("NIFTY500 HEALTHCARE"):
        return "sector"
    body = " " + n[len("NIFTY"):].strip() if n.startswith("NIFTY") else " " + n
    if any(body.strip() == w.strip() or body.startswith(" " + w.strip() + " ") or body.strip().startswith(w.strip())
           for w in SECTORAL_WORDS):
        return "sector"
    return "thematic"


# --------------------------------------------------------------------------- daily index files
def close_all_url(d: dt.date) -> str:
    return f"{nse.NSE_ARCHIVES}/content/indices/ind_close_all_{d:%d%m%Y}.csv"


def parse_close_all(content: bytes, expect: dt.date | None = None) -> pd.DataFrame | None:
    df = nse._read_csv(content)
    cols = {c.lower().strip(): c for c in df.columns}
    name_c = cols.get("index name")
    date_c = cols.get("index date")
    if not name_c or not date_c or df.empty:
        return None
    if expect is not None:
        d0 = pd.to_datetime(df[date_c].iloc[0], format="%d-%m-%Y", errors="coerce")
        if pd.isna(d0) or d0.date() != expect:
            return None  # NSE re-served another session's file under this date
    pick = lambda key: nse._num(df[cols[key]]) if key in cols else float("nan")  # noqa: E731
    out = pd.DataFrame({
        "name": df[name_c].str.strip(),
        "open": pick("open index value"),
        "high": pick("high index value"),
        "low": pick("low index value"),
        "close": pick("closing index value"),
        "volume": pick("volume"),
        "turnover_cr": pick("turnover (rs. cr.)"),
        "pe": pick("p/e"),
        "pb": pick("p/b"),
        "dy": pick("div yield"),
    })
    return out[out["close"].notna()]


class IndexStore:
    def __init__(self, root: Path):
        self.root = Path(root) / "indices"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, d: dt.date) -> Path:
        return self.root / f"{d:%Y}" / f"{d:%Y%m%d}.csv.gz"

    def existing_dates(self) -> set[dt.date]:
        out = set()
        for p in self.root.glob("*/*.csv.gz"):
            try:
                out.add(dt.datetime.strptime(p.name[:8], "%Y%m%d").date())
            except ValueError:
                pass
        return out

    def write_day(self, d: dt.date, frame: pd.DataFrame) -> None:
        p = self.path_for(d)
        p.parent.mkdir(parents=True, exist_ok=True)
        frame[INDEX_COLUMNS].to_csv(p, index=False, compression={"method": "gzip", "mtime": 0},
                                    float_format="%.10g")

    def prune(self, keep_from: dt.date) -> int:
        n = 0
        for p in self.root.glob("*/*.csv.gz"):
            try:
                if dt.datetime.strptime(p.name[:8], "%Y%m%d").date() < keep_from:
                    p.unlink()
                    n += 1
            except ValueError:
                pass
        return n

    def load(self, start: dt.date) -> pd.DataFrame:
        frames = []
        for p in sorted(self.root.glob("*/*.csv.gz")):
            d = dt.datetime.strptime(p.name[:8], "%Y%m%d").date()
            if d < start:
                continue
            f = pd.read_csv(p, keep_default_na=False, na_values={c: [""] for c in INDEX_COLUMNS[1:]})
            f["date"] = pd.Timestamp(d)
            frames.append(f)
        if not frames:
            return pd.DataFrame(columns=INDEX_COLUMNS + ["date", "code"])
        df = pd.concat(frames, ignore_index=True)
        for c in INDEX_COLUMNS[1:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["name"] = df["name"].astype(str)
        df["code"] = df["name"].map(index_code)
        return df


def update_index_store(store: IndexStore, sessions: list[dt.date], session, workers: int = 3) -> list[dt.date]:
    """Download the index file for every stock trading session that is not stored yet."""
    have = store.existing_dates()
    todo = sorted(d for d in sessions if d not in have)
    if not todo:
        log("index store up to date")
        return []
    log(f"downloading {len(todo)} index file(s): {todo[0]} .. {todo[-1]}")

    def job(day):
        try:
            raw = nse.http_get(session, close_all_url(day))
            return day, (parse_close_all(raw, day) if raw is not None else None), None
        except Exception as exc:
            return day, None, exc

    added, missing, errors = [], 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for day, frame, err in ex.map(job, todo):
            if err is not None:
                errors += 1
                if errors <= 5:
                    log(f"{day}: download error ({err}) - will retry next run")
                continue
            if frame is None or frame.empty:
                missing += 1
                continue
            store.write_day(day, frame)
            added.append(day)
    log(f"added {len(added)} index day(s); {missing} not published yet; {errors} error(s)")
    return added


# --------------------------------------------------------------------------- constituents
def _parse_members(raw: bytes) -> tuple[list[str], dict[str, str]] | None:
    try:
        df = nse._read_csv(raw)
    except Exception:
        return None
    cols = {c.strip().lower(): c for c in df.columns}
    sym = cols.get("symbol")
    if not sym:
        return None
    syms = [s for s in df[sym].astype(str).str.strip() if s and s.upper() != "SYMBOL"]
    ind = {}
    if "industry" in cols:
        ind = {s: i for s, i in zip(df[sym].astype(str).str.strip(), df[cols["industry"]].astype(str).str.strip())}
    return (syms, ind) if syms else None


def load_members(meta: Path) -> dict:
    p = Path(meta) / "sector_members.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"fetched": None, "members": {}, "source": {}, "industry": {}}


def update_members(meta: Path, session, now: dt.datetime, force: bool = False) -> dict:
    """Refresh constituent lists (at most once a day; a failed file keeps its cached list)."""
    data = load_members(meta)
    last = data.get("fetched")
    if not force and last and last[:10] == now.strftime("%Y-%m-%d"):
        return data
    members, source, industry = dict(data.get("members", {})), dict(data.get("source", {})), dict(data.get("industry", {}))
    ok = 0
    for code, files in MEMBER_FILES.items():
        for fname in files:
            try:
                raw = nse.http_get(session, f"{nse.NSE_ARCHIVES}/content/indices/{fname}")
            except Exception as exc:
                log(f"{fname}: {exc}")
                raw = None
            if raw is None:
                continue
            parsed = _parse_members(raw)
            if parsed:
                members[code], ind = parsed
                industry.update(ind)
                source[code] = fname
                ok += 1
                break
        time.sleep(0.15)
    data = {"fetched": now.strftime("%Y-%m-%d %H:%M"), "members": dict(sorted(members.items())),
            "source": dict(sorted(source.items())), "industry": dict(sorted(industry.items()))}
    (Path(meta) / "sector_members.json").write_text(json.dumps(data, indent=0))
    log(f"constituent lists: {ok} refreshed, {len(members)} available")
    return data
