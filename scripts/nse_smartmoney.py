"""
Smart-money data stores for the Smart Money & Institutional page (site/smartmoney.html).

All sources are public NSE files. The archive host (nsearchives.nseindia.com) needs no login or cookies;
the two www.nseindia.com APIs (FII/DII cash flows, historical deals) need a cookie handshake and are used
on a best-effort basis only - the page works without them.

  data/sm/deliv/YYYY/YYYYMMDD.csv.gz  security-wise delivery (sec_bhavdata_full): deliverable qty / %, VWAP, trades
  data/sm/fo/YYYY/YYYYMMDD.csv.gz     F&O UDiFF bhavcopy summarised per underlying: futures OI, change, volume,
                                      option CE/PE open interest
  data/sm/participant_oi.csv          participant-wise open interest (Client / DII / FII / Pro), one row per day+type
  data/sm/deals.csv.gz                bulk + block deals (daily files, plus a best-effort history back-fill)
  data/sm/fiidii.csv                  FII/FPI and DII cash-market buy / sell / net (Rs crore), accumulated daily
"""
from __future__ import annotations

import datetime as dt
import io
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import nse_data as nse

WWW = os.environ.get("NSE_WWW", "https://www.nseindia.com").rstrip("/")
DELIV_COLS = ["symbol", "series", "close", "qty", "trades", "dq", "dp", "ap"]
FO_COLS = ["symbol", "kind", "fut_close", "spot", "lot", "fut_oi", "fut_oi_nx", "fut_doi_nx", "fut_vol", "fut_val",
           "ce_oi", "pe_oi", "ce_doi", "pe_doi", "ce_vol", "pe_vol"]
PART_COLS = ["date", "type", "fut_idx_long", "fut_idx_short", "fut_stk_long", "fut_stk_short",
             "opt_idx_call_long", "opt_idx_put_long", "opt_idx_call_short", "opt_idx_put_short",
             "opt_stk_call_long", "opt_stk_put_long", "opt_stk_call_short", "opt_stk_put_short",
             "total_long", "total_short"]
DEAL_COLS = ["date", "kind", "symbol", "name", "client", "side", "qty", "price"]

# Per-run download caps keep each workflow run short and polite to NSE; a long back-fill simply
# completes over the next few scheduled runs.
MAX_DELIV_PER_RUN = 520
MAX_FO_PER_RUN = 140
MAX_PART_PER_RUN = 300
MAX_DEAL_API_CALLS = 40


def log(msg: str) -> None:
    print(f"[sm-data] {msg}", flush=True)


def fo_url(d: dt.date) -> str:
    return f"{nse.NSE_ARCHIVES}/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"


def part_url(d: dt.date) -> str:
    return f"{nse.NSE_ARCHIVES}/content/nsccl/fao_participant_oi_{d:%d%m%Y}.csv"


# --------------------------------------------------------------------------- parsers
def parse_delivery(content: bytes, expect: dt.date | None) -> pd.DataFrame | None:
    """sec_bhavdata_full -> symbol, series, close, qty, trades, dq (deliverable qty), dp (deliv %), ap (VWAP)."""
    df = nse._read_csv(content)
    if df.empty or "DELIV_QTY" not in df.columns:
        return None
    if expect is not None:
        d1 = pd.to_datetime(df["DATE1"].iloc[0], format="%d-%b-%Y", errors="coerce")
        if pd.isna(d1) or d1.date() != expect:
            return None  # NSE re-serves the previous session's file on holidays
    df = df[df["SERIES"].isin(nse.EQUITY_SERIES)]
    out = pd.DataFrame({
        "symbol": df["SYMBOL"].values,
        "series": df["SERIES"].values,
        "close": nse._num(df["CLOSE_PRICE"]).values,
        "qty": nse._num(df["TTL_TRD_QNTY"]).values,
        "trades": nse._num(df["NO_OF_TRADES"]).values,
        "dq": nse._num(df["DELIV_QTY"]).values,
        "dp": nse._num(df["DELIV_PER"]).values,
        "ap": nse._num(df["AVG_PRICE"]).values,
    })
    return out[DELIV_COLS]


def parse_fo(content: bytes, expect: dt.date | None) -> pd.DataFrame | None:
    """F&O UDiFF bhavcopy -> one row per underlying (stock or index)."""
    members = nse._zip_members(content)
    raw = next(iter(members.values()))
    df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    if df.empty or "FinInstrmTp" not in df.columns:
        return None
    if expect is not None and df["TradDt"].iloc[0] != expect.isoformat():
        return None
    for c in ("OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "ClsPric", "UndrlygPric", "NewBrdLotQty"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    tday = df["TradDt"].iloc[0]
    fut = df[df["FinInstrmTp"].isin(["STF", "IDF"])].copy()
    opt = df[df["FinInstrmTp"].isin(["STO", "IDO"])].copy()
    if fut.empty:
        return None
    fut["nx"] = fut["XpryDt"] > tday  # contracts that do not expire today
    fut = fut.sort_values(["TckrSymb", "XpryDt"])
    g = fut.groupby("TckrSymb")
    near = g.first()
    nx = fut[fut["nx"]].groupby("TckrSymb")
    out = pd.DataFrame({
        "symbol": near.index,
        "kind": np.where(near["FinInstrmTp"].values == "IDF", "I", "S"),
        "fut_close": near["ClsPric"].values,
        "spot": near["UndrlygPric"].values,
        "lot": near["NewBrdLotQty"].values,
        "fut_oi": g["OpnIntrst"].sum().values,
        "fut_vol": g["TtlTradgVol"].sum().values,
        "fut_val": g["TtlTrfVal"].sum().values,
    }).set_index("symbol")
    out["fut_oi_nx"] = nx["OpnIntrst"].sum()
    out["fut_doi_nx"] = nx["ChngInOpnIntrst"].sum()
    if not opt.empty:
        for side, tag in (("CE", "ce"), ("PE", "pe")):
            o = opt[opt["OptnTp"] == side].groupby("TckrSymb")
            out[f"{tag}_oi"] = o["OpnIntrst"].sum()
            out[f"{tag}_doi"] = o["ChngInOpnIntrst"].sum()
            out[f"{tag}_vol"] = o["TtlTradgVol"].sum()
    for c in FO_COLS:
        if c not in out.columns and c != "symbol":
            out[c] = np.nan
    return out.reset_index()[FO_COLS]


def parse_participant(content: bytes, d: dt.date) -> pd.DataFrame | None:
    text = content.decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    hdr = next((i for i, ln in enumerate(lines) if ln.lower().startswith("client type")), None)
    if hdr is None:
        return None
    title = " ".join(lines[:hdr])
    try:  # "... as on Nov 20, 2026"
        on = dt.datetime.strptime(title.split("as on", 1)[1].strip(' ",'), "%b %d, %Y").date()
        if on != d:
            return None
    except Exception:
        pass
    rows = []
    for ln in lines[hdr + 1:]:
        p = [x.strip().strip('"') for x in ln.split(",")]
        if len(p) < 15 or not p[0]:
            continue
        vals = pd.to_numeric(pd.Series(p[1:15]), errors="coerce").tolist()
        rows.append([d.isoformat(), p[0].upper()] + vals)
    if not rows:
        return None
    return pd.DataFrame(rows, columns=PART_COLS)


def parse_deals_csv(content: bytes, kind: str) -> pd.DataFrame | None:
    df = nse._read_csv(content)
    if df.empty:
        return None
    cols = {c.lower(): c for c in df.columns}
    get = lambda *keys: next((cols[k] for k in cols if any(k2 in k for k2 in keys)), None)  # noqa: E731
    c_date, c_sym, c_name = get("date"), get("symbol"), get("security name")
    c_cli, c_side, c_qty, c_px = get("client"), get("buy/sell", "buy / sell"), get("quantity"), get("price")
    if not all([c_date, c_sym, c_cli, c_side, c_qty, c_px]):
        return None
    out = pd.DataFrame({
        "date": pd.to_datetime(df[c_date], format="%d-%b-%Y", errors="coerce").dt.strftime("%Y-%m-%d"),
        "kind": kind,
        "symbol": df[c_sym].str.upper(),
        "name": df[c_name] if c_name else "",
        "client": df[c_cli].str.upper().str.replace(r"\s+", " ", regex=True),
        "side": df[c_side].str.upper().str[0],
        "qty": nse._num(df[c_qty]),
        "price": nse._num(df[c_px]),
    })
    return out.dropna(subset=["date"])


def parse_deals_api(rows: list, kind: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=DEAL_COLS)
    df = pd.DataFrame(rows)
    return pd.DataFrame({
        "date": pd.to_datetime(df.get("BD_DT_DATE"), format="%d-%b-%Y", errors="coerce").dt.strftime("%Y-%m-%d"),
        "kind": kind,
        "symbol": df.get("BD_SYMBOL", "").astype(str).str.upper(),
        "name": df.get("BD_SCRIP_NAME", ""),
        "client": df.get("BD_CLIENT_NAME", "").astype(str).str.upper().str.replace(r"\s+", " ", regex=True),
        "side": df.get("BD_BUY_SELL", "").astype(str).str.upper().str[0],
        "qty": pd.to_numeric(df.get("BD_QTY_TRD"), errors="coerce"),
        "price": pd.to_numeric(df.get("BD_TP_WATP"), errors="coerce"),
    }).dropna(subset=["date"])


# --------------------------------------------------------------------------- store
class SmStore:
    def __init__(self, data_root: Path):
        self.root = Path(data_root) / "sm"
        self.deliv = self.root / "deliv"
        self.fo = self.root / "fo"
        for p in (self.root, self.deliv, self.fo):
            p.mkdir(parents=True, exist_ok=True)
        self.part_file = self.root / "participant_oi.csv"
        self.deals_file = self.root / "deals.csv.gz"
        self.fiidii_file = self.root / "fiidii.csv"
        self.missing_file = self.root / "missing.txt"     # "kind:YYYY-MM-DD" lines NSE never published
        self.deal_days_file = self.root / "deal_days.txt"  # "kind:YYYY-MM-DD" lines back-filled from the API

    # ---- generic helpers
    @staticmethod
    def _dates_in(folder: Path) -> set[dt.date]:
        out = set()
        for p in folder.glob("*/*.csv.gz"):
            try:
                out.add(dt.datetime.strptime(p.name[:8], "%Y%m%d").date())
            except ValueError:
                pass
        return out

    @staticmethod
    def _path(folder: Path, d: dt.date) -> Path:
        return folder / f"{d:%Y}" / f"{d:%Y%m%d}.csv.gz"

    def _write(self, folder: Path, d: dt.date, frame: pd.DataFrame) -> None:
        p = self._path(folder, d)
        p.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(p, index=False, compression={"method": "gzip", "mtime": 0}, float_format="%.10g")

    def _lines(self, p: Path) -> set[str]:
        return {x.strip() for x in p.read_text().split()} if p.exists() else set()

    def _save_lines(self, p: Path, s: set[str]) -> None:
        p.write_text("\n".join(sorted(s)) + "\n")

    def prune(self, keep_from: dt.date) -> None:
        for folder in (self.deliv, self.fo):
            for p in folder.glob("*/*.csv.gz"):
                try:
                    if dt.datetime.strptime(p.name[:8], "%Y%m%d").date() < keep_from:
                        p.unlink()
                except ValueError:
                    pass

    # ---- loaders
    def load_deliv(self, start: dt.date) -> pd.DataFrame:
        frames = []
        for p in sorted(self.deliv.glob("*/*.csv.gz")):
            d = dt.datetime.strptime(p.name[:8], "%Y%m%d").date()
            if d < start:
                continue
            f = pd.read_csv(p, dtype={"symbol": str, "series": str}, keep_default_na=False, na_values={c: [""] for c in DELIV_COLS[2:]})
            f["date"] = pd.Timestamp(d)
            frames.append(f)
        if not frames:
            return pd.DataFrame(columns=DELIV_COLS + ["date"])
        df = pd.concat(frames, ignore_index=True)
        for c in DELIV_COLS[2:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    def load_fo(self, start: dt.date) -> pd.DataFrame:
        frames = []
        for p in sorted(self.fo.glob("*/*.csv.gz")):
            d = dt.datetime.strptime(p.name[:8], "%Y%m%d").date()
            if d < start:
                continue
            f = pd.read_csv(p, dtype={"symbol": str, "kind": str}, keep_default_na=False, na_values={c: [""] for c in FO_COLS[2:]})
            f["date"] = pd.Timestamp(d)
            frames.append(f)
        if not frames:
            return pd.DataFrame(columns=FO_COLS + ["date"])
        df = pd.concat(frames, ignore_index=True)
        for c in FO_COLS[2:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    def load_participant(self) -> pd.DataFrame:
        if not self.part_file.exists():
            return pd.DataFrame(columns=PART_COLS)
        return pd.read_csv(self.part_file)

    def load_deals(self) -> pd.DataFrame:
        if not self.deals_file.exists():
            return pd.DataFrame(columns=DEAL_COLS)
        return pd.read_csv(self.deals_file, dtype={"symbol": str, "client": str, "name": str}, keep_default_na=False,
                           na_values={"qty": [""], "price": [""]})

    def load_fiidii(self) -> pd.DataFrame:
        if not self.fiidii_file.exists():
            return pd.DataFrame(columns=["date", "cat", "buy", "sell", "net"])
        return pd.read_csv(self.fiidii_file)

    # ---- savers for accumulated tables
    def add_participant(self, frame: pd.DataFrame) -> None:
        old = self.load_participant()
        new = pd.concat([old, frame], ignore_index=True).drop_duplicates(["date", "type"], keep="last")
        new.sort_values(["date", "type"]).to_csv(self.part_file, index=False, float_format="%.10g")

    def add_deals(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        old = self.load_deals()
        frame = frame[DEAL_COLS].copy()
        new = pd.concat([old, frame], ignore_index=True)
        new["qty"] = pd.to_numeric(new["qty"], errors="coerce")
        new["price"] = pd.to_numeric(new["price"], errors="coerce")
        new = new.drop_duplicates(["date", "kind", "symbol", "client", "side", "qty", "price"])
        added = len(new) - len(old)
        new.sort_values(["date", "kind", "symbol", "client"]).to_csv(
            self.deals_file, index=False, compression={"method": "gzip", "mtime": 0}, float_format="%.10g")
        return added

    def add_fiidii(self, frame: pd.DataFrame) -> None:
        old = self.load_fiidii()
        new = pd.concat([old, frame], ignore_index=True).drop_duplicates(["date", "cat"], keep="last")
        new.sort_values(["date", "cat"]).to_csv(self.fiidii_file, index=False)


# --------------------------------------------------------------------------- download jobs
def _run_jobs(name, dates, fetch, store: SmStore, today: dt.date, workers: int, on_result):
    if not dates:
        return 0
    log(f"{name}: downloading {len(dates)} day(s) {dates[0]} .. {dates[-1]}")
    missing = store._lines(store.missing_file)
    added = errors = 0

    def job(d):
        try:
            return d, fetch(d), None
        except Exception as exc:  # 403 / timeouts: retried next run
            return d, None, exc

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for d, res, err in ex.map(job, dates):
            if err is not None:
                errors += 1
                if errors <= 3:
                    log(f"{name} {d}: {err}")
                continue
            if res is None:
                if (today - d).days >= 5:
                    missing.add(f"{name}:{d.isoformat()}")
                continue
            on_result(d, res)
            added += 1
    store._save_lines(store.missing_file, missing)
    log(f"{name}: added {added}, errors {errors}")
    return added


def update_delivery(store: SmStore, sessions: list[dt.date], today: dt.date, session: requests.Session, workers=3) -> int:
    have = store._dates_in(store.deliv)
    miss = store._lines(store.missing_file)
    todo = [d for d in sessions if d not in have and f"deliv:{d.isoformat()}" not in miss]
    todo = sorted(todo, reverse=True)[:MAX_DELIV_PER_RUN]  # newest first, so the page is useful after run 1

    def fetch(d):
        raw = nse.http_get(session, nse.secbhav_url(d))
        return parse_delivery(raw, d) if raw is not None else None

    return _run_jobs("deliv", sorted(todo), fetch, store, today, workers, lambda d, f: store._write(store.deliv, d, f))


def update_fo(store: SmStore, sessions: list[dt.date], today: dt.date, session: requests.Session, workers=3) -> int:
    have = store._dates_in(store.fo)
    miss = store._lines(store.missing_file)
    todo = [d for d in sessions if d not in have and f"fo:{d.isoformat()}" not in miss]
    todo = sorted(todo, reverse=True)[:MAX_FO_PER_RUN]

    def fetch(d):
        raw = nse.http_get(session, fo_url(d))
        return parse_fo(raw, d) if raw is not None else None

    return _run_jobs("fo", sorted(todo), fetch, store, today, workers, lambda d, f: store._write(store.fo, d, f))


def update_participant(store: SmStore, sessions: list[dt.date], today: dt.date, session: requests.Session, workers=3) -> int:
    part = store.load_participant()
    have = set(pd.to_datetime(part["date"]).dt.date) if len(part) else set()
    miss = store._lines(store.missing_file)
    todo = [d for d in sessions if d not in have and f"part:{d.isoformat()}" not in miss]
    todo = sorted(todo, reverse=True)[:MAX_PART_PER_RUN]
    got = []

    def fetch(d):
        raw = nse.http_get(session, part_url(d))
        return parse_participant(raw, d) if raw is not None else None

    n = _run_jobs("part", sorted(todo), fetch, store, today, workers, lambda d, f: got.append(f))
    if got:
        store.add_participant(pd.concat(got, ignore_index=True))
    return n


def update_daily_deals(store: SmStore, session: requests.Session) -> int:
    frames = []
    for kind, fname in (("bulk", "bulk.csv"), ("block", "block.csv")):
        try:
            raw = nse.http_get(session, f"{nse.NSE_ARCHIVES}/content/equities/{fname}")
            f = parse_deals_csv(raw, kind) if raw is not None else None
            if f is not None and len(f):
                frames.append(f)
        except Exception as exc:
            log(f"{fname}: {exc}")
    n = store.add_deals(pd.concat(frames, ignore_index=True)) if frames else 0
    log(f"daily bulk/block deals: {n} new row(s)")
    return n


def www_session(session: requests.Session) -> bool:
    """Cookie handshake for www.nseindia.com JSON APIs (best effort)."""
    try:
        r = session.get(f"{WWW}/report-detail/display-bulk-and-block-deals", timeout=20)
        return r.status_code == 200
    except Exception as exc:
        log(f"www.nseindia.com unreachable ({exc.__class__.__name__}) - skipping cookie APIs")
        return False


def _www_json(session: requests.Session, path: str):
    time.sleep(0.8)
    r = session.get(f"{WWW}{path}", timeout=25, headers={"Accept": "application/json, text/plain, */*",
                                                         "X-Requested-With": "XMLHttpRequest"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError("non-JSON answer (blocked)")


def update_fiidii(store: SmStore, session: requests.Session) -> int:
    try:
        rows = _www_json(session, "/api/fiidiiTradeReact")
    except Exception as exc:
        log(f"FII/DII cash: {exc}")
        return 0
    recs = []
    for r in rows or []:
        try:
            d = dt.datetime.strptime(r["date"], "%d-%b-%Y").date().isoformat()
            cat = "FII" if "FII" in r["category"].upper() or "FPI" in r["category"].upper() else "DII"
            recs.append({"date": d, "cat": cat, "buy": float(r["buyValue"]), "sell": float(r["sellValue"]),
                         "net": float(r["netValue"])})
        except Exception:
            continue
    if recs:
        store.add_fiidii(pd.DataFrame(recs))
    log(f"FII/DII cash: {len(recs)} row(s)")
    return len(recs)


def backfill_deals(store: SmStore, sessions: list[dt.date], session: requests.Session) -> int:
    """One API call per (kind, day), newest first, at most MAX_DEAL_API_CALLS per run; stops at the first block."""
    done = store._lines(store.deal_days_file)
    have = store.load_deals()
    have_keys = set(zip(have["kind"], have["date"])) if len(have) else set()
    todo = []
    for d in sorted(sessions, reverse=True):
        for kind in ("bulk", "block"):
            key = f"{kind}:{d.isoformat()}"
            if key not in done and (kind, d.isoformat()) not in have_keys:
                todo.append((kind, d))
    todo = todo[:MAX_DEAL_API_CALLS]
    if not todo:
        return 0
    frames, ok = [], 0
    for kind, d in todo:
        ds = d.strftime("%d-%m-%Y")
        try:
            j = _www_json(session, f"/api/historicalOR/bulk-block-short-deals?optionType={kind}_deals&from={ds}&to={ds}")
        except Exception as exc:
            log(f"deal history back-fill stopped ({exc})")
            break
        frames.append(parse_deals_api((j or {}).get("data") or [], kind))
        done.add(f"{kind}:{d.isoformat()}")
        ok += 1
    if frames:
        store.add_deals(pd.concat(frames, ignore_index=True))
    store._save_lines(store.deal_days_file, done)
    log(f"deal history back-fill: {ok} day-call(s)")
    return ok


def update_all(store: SmStore, sessions: list[dt.date], today: dt.date, session: requests.Session,
               deliv_from: dt.date, fo_days: int = 260, workers: int = 3) -> None:
    sess = sorted(sessions)
    update_delivery(store, [d for d in sess if d >= deliv_from], today, session, workers)
    update_fo(store, sess[-fo_days:], today, session, workers)
    update_participant(store, sess[-fo_days:], today, session, workers)
    update_daily_deals(store, session)
    if www_session(session):
        update_fiidii(store, session)
        backfill_deals(store, [d for d in sess if (today - d).days <= 370], session)
