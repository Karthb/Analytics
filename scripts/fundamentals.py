"""
Fundamentals (Screener.in-style ratios) for every NSE stock in ONE request.

Uses TradingView's public screener endpoint (the same one its stock screener page calls).
It is unofficial: if it ever changes or fails, the screener still works with NSE price
data and pivot levels - fundamentals columns just show "-".
"""
from __future__ import annotations

import os
import re

import pandas as pd
import requests

TV_SCAN_URL = os.environ.get("TV_SCAN_URL", "https://scanner.tradingview.com/india/scan")

# our key -> TradingView field
FIELDS = {
    "tv_name": "name",
    "tv_desc": "description",
    "tv_type": "type",
    "sector": "sector",
    "industry": "industry",
    "tv_open": "open",
    "tv_high": "high",
    "tv_low": "low",
    "tv_close": "close",
    "tv_volume": "volume",
    "tv_mcap": "market_cap_basic",
    "tv_pe": "price_earnings_ttm",
    "bv": "book_value_per_share_fq",
    "dy": "dividends_yield_current",
    "roe": "return_on_equity_fy",
    "roe_ttm": "return_on_equity",
    "roce": "return_on_capital_employed_fy",
    "roic": "return_on_invested_capital",
    "roa": "return_on_assets",
    "eps": "earnings_per_share_diluted_ttm",
    "de": "debt_to_equity_fq",
    "sales_g": "total_revenue_yoy_growth_ttm",
    "sales_g_fy": "total_revenue_yoy_growth_fy",
    "profit_g": "net_income_yoy_growth_ttm",
    "profit_g_fy": "net_income_yoy_growth_fy",
    "eps_g": "earnings_per_share_diluted_yoy_growth_ttm",
    "peg": "price_earnings_growth_ttm",
    "pio": "piotroski_f_score_fy",
    "sales_ttm": "total_revenue_ttm",
    "profit_ttm": "net_income_ttm",
    "fcf_ttm": "free_cash_flow_ttm",
    "fcf_fy": "free_cash_flow_fy",
    "holders": "number_of_shareholders",
    "opm": "operating_margin_ttm",
    "npm": "net_margin_ttm",
    "cur_ratio": "current_ratio_fq",
    "int_cov": "interst_cover_fy",  # (sic) TradingView's field name
    "ev_ebitda": "enterprise_value_ebitda_ttm",
    "beta": "beta_1_year",
    "float_pct": "float_shares_percent_current",
    "payout": "dividend_payout_ratio_ttm",
    "next_res": "earnings_release_next_date",
    "tv_shares": "total_shares_outstanding_fundamental",
}


def log(msg: str) -> None:
    print(f"[fundamentals] {msg}", flush=True)


def tv_key(nse_symbol: str) -> str:
    """NSE symbol -> TradingView ticker name (BAJAJ-AUTO -> BAJAJ_AUTO; M&M stays M&M).
    TradingView keeps "&" (checked 23 Nov 2026: M&M, J&KBANK, GVT&D, IL&FSENGG ...)."""
    return re.sub(r"[^A-Z0-9&]", "_", str(nse_symbol).upper())


def fetch_tradingview(session: requests.Session) -> pd.DataFrame | None:
    fields = list(FIELDS.values())
    payload = {
        "filter": [{"left": "exchange", "operation": "equal", "right": "NSE"}],
        "options": {"lang": "en"},
        "markets": ["india"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": fields,
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, 10000],
    }
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://in.tradingview.com",
        "Referer": "https://in.tradingview.com/",
    }
    try:
        r = session.post(TV_SCAN_URL, json=payload, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json().get("data") or []
    except Exception as exc:
        log(f"request failed: {exc}")
        return None
    rows = []
    for item in data:
        sym = str(item.get("s", "")).split(":", 1)[-1]
        vals = item.get("d") or []
        row = {"tv": sym}
        row.update({k: (vals[i] if i < len(vals) else None) for i, k in enumerate(FIELDS)})
        rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        log("empty response")
        return None
    df = df.drop_duplicates("tv")
    log(f"{len(df)} NSE instruments")
    return df
