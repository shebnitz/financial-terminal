"""
sec_edgar.py
============
This module knows how to talk to SEC EDGAR and turn its raw XBRL data
into clean pandas tables (a "DataFrame") for a balance sheet, income
statement, or cash flow statement.

It has no GUI code in it at all -- app.py (the Streamlit app) is the
only file that imports this one and displays anything. Keeping "get the
data" separate from "show the data" is a habit worth building early:
it means you can test this file on its own (see the bottom of this
file), and swap the GUI for something else later without touching any
of the data logic.

SEC EDGAR basics, in case you're new to it (most people are):
- Every public company has a CIK (Central Index Key), a stable ID number
  SEC assigns them -- separate from its stock ticker.
- The "company facts" API returns, for one company, every number it has
  ever reported to SEC, tagged with a standardized accounting concept
  name (e.g. "Assets", "NetIncomeLoss") from a taxonomy called US-GAAP.
- We don't have to guess these tag names: they come from the official
  XBRL taxonomy that every US public company's filings use, which is
  exactly why this works the same way for META, AAPL, or any other
  ticker.
- None of this needs an API key. It's free and open. The only rule is
  that we identify ourselves via a User-Agent header (see config.py).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import pandas as pd
import requests

import config

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL_TMPL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"

HEADERS = {"User-Agent": config.USER_AGENT}


class SecEdgarError(Exception):
    """Raised when we can't get a clean answer back from SEC EDGAR."""


# ---------------------------------------------------------------------------
# Low-level plumbing: cached HTTP GETs
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> str:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    safe_key = key.replace("/", "_").replace(":", "_")
    return os.path.join(config.CACHE_DIR, f"{safe_key}.json")


def _get_json(url: str, cache_key: str) -> dict:
    """GET a URL as JSON, using an on-disk cache so repeated Streamlit
    reruns don't hammer SEC's servers (and so the app feels instant on
    the second load)."""
    path = _cache_path(cache_key)

    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < config.CACHE_TTL_SECONDS:
        with open(path, "r") as f:
            return json.load(f)

    if config.EDGAR_CONTACT_EMAIL == "you@example.com":
        raise SecEdgarError(
            "You still have the placeholder contact info in config.py (or .env). "
            "SEC requires a real name and email in the User-Agent header -- "
            "open config.py (or your .env file) and fill in your own details."
        )

    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code == 404:
        raise SecEdgarError(f"SEC EDGAR returned 404 for {url}. Double check the ticker/CIK.")
    if resp.status_code == 403:
        raise SecEdgarError(
            "SEC EDGAR returned 403 (forbidden). This almost always means the "
            "User-Agent header was rejected -- check EDGAR_CONTACT_EMAIL in "
            "config.py / .env."
        )
    resp.raise_for_status()

    data = resp.json()
    with open(path, "w") as f:
        json.dump(data, f)
    return data


# ---------------------------------------------------------------------------
# Ticker -> CIK lookup
# ---------------------------------------------------------------------------

def get_cik_for_ticker(ticker: str) -> str:
    """Look up a company's 10-digit, zero-padded CIK from its ticker
    symbol, e.g. 'META' -> '0001326801'."""
    ticker = ticker.strip().upper()
    data = _get_json(TICKERS_URL, cache_key="company_tickers")

    for row in data.values():
        if row["ticker"] == ticker:
            return f"{row['cik_str']:010d}"

    raise SecEdgarError(f"Couldn't find ticker '{ticker}' in SEC's company list. Check the spelling.")


# ---------------------------------------------------------------------------
# Company facts (all XBRL data SEC has for one company)
# ---------------------------------------------------------------------------

def get_company_facts(cik: str) -> dict:
    """Download (or load from cache) the full XBRL 'company facts' blob
    for one company. This can be a few MB for a large company -- it's
    every number they've ever reported, for every tag."""
    url = FACTS_URL_TMPL.format(cik=int(cik))
    return _get_json(url, cache_key=f"facts_{cik}")


# ---------------------------------------------------------------------------
# Statement line-item definitions
# ---------------------------------------------------------------------------
# Companies don't all use the exact same XBRL tag for the same line item
# (e.g. some tag revenue as "Revenues", others as
# "RevenueFromContractWithCustomerExcludingAssessedTax"). For each line
# item we list candidate tags in priority order and use the first one
# that has data for the period we're building.
#
# "instant" tags are balance-sheet-style: a snapshot at one date.
# "duration" tags are income/cash-flow-style: a total over a period
# (e.g. the quarter from 2026-04-01 to 2026-06-30).

@dataclass
class LineItem:
    label: str
    tags: list[str]
    kind: str  # "instant" or "duration"


STATEMENTS: dict[str, list[LineItem]] = {
    "Balance Sheet": [
        LineItem("Cash and cash equivalents", ["CashAndCashEquivalentsAtCarryingValue", "Cash"], "instant"),
        LineItem("Short-term investments", ["ShortTermInvestments", "MarketableSecuritiesCurrent"], "instant"),
        LineItem("Total current assets", ["AssetsCurrent"], "instant"),
        LineItem("Property and equipment, net", ["PropertyPlantAndEquipmentNet"], "instant"),
        LineItem("Total assets", ["Assets"], "instant"),
        LineItem("Total current liabilities", ["LiabilitiesCurrent"], "instant"),
        LineItem("Long-term debt", ["LongTermDebtNoncurrent", "LongTermDebt"], "instant"),
        LineItem("Total liabilities", ["Liabilities"], "instant"),
        LineItem("Total stockholders' equity", ["StockholdersEquity"], "instant"),
        LineItem("Total liabilities and equity", ["LiabilitiesAndStockholdersEquity"], "instant"),
    ],
    "Income Statement": [
        LineItem("Total revenue", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"], "duration"),
        LineItem("Cost of revenue", ["CostOfRevenue", "CostsAndExpenses"], "duration"),
        LineItem("Gross profit", ["GrossProfit"], "duration"),
        LineItem("Research and development", ["ResearchAndDevelopmentExpense"], "duration"),
        LineItem("Selling, general and administrative", ["SellingGeneralAndAdministrativeExpense"], "duration"),
        LineItem("Operating income", ["OperatingIncomeLoss"], "duration"),
        LineItem("Net income", ["NetIncomeLoss"], "duration"),
        LineItem("Diluted earnings per share", ["EarningsPerShareDiluted"], "duration"),
    ],
    "Cash Flow Statement": [
        LineItem("Net cash from operating activities", ["NetCashProvidedByUsedInOperatingActivities"], "duration"),
        LineItem("Net cash from investing activities", ["NetCashProvidedByUsedInInvestingActivities"], "duration"),
        LineItem("Net cash from financing activities", ["NetCashProvidedByUsedInFinancingActivities"], "duration"),
        LineItem("Capital expenditures", ["PaymentsToAcquirePropertyPlantAndEquipment"], "duration"),
        LineItem(
            "Net change in cash",
            [
                "CashAndCashEquivalentsPeriodIncreaseDecrease",
                "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
            ],
            "duration",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Building a statement DataFrame
# ---------------------------------------------------------------------------

def _facts_for_tag(company_facts: dict, tag: str) -> list[dict]:
    """Pull the raw list of reported values for one us-gaap tag out of a
    company-facts blob, in USD. Returns [] if the company never used
    this tag."""
    try:
        return company_facts["facts"]["us-gaap"][tag]["units"]["USD"]
    except KeyError:
        return []


def _quarterly_points(entries: list[dict], kind: str) -> dict[str, dict]:
    """Reduce raw fact entries down to one value per fiscal quarter,
    keyed by period end date, keeping only 10-Q / 10-K filed values
    (skips restated/duplicate values from other filings when possible
    by preferring the most-recently-filed one)."""
    by_end: dict[str, dict] = {}
    for e in entries:
        if e.get("form") not in ("10-Q", "10-K"):
            continue
        if kind == "duration":
            # Keep roughly-quarterly durations only (skip 6-month,
            # 9-month, or full-year cumulative entries that share the
            # same tag in a 10-Q/10-K).
            start = e.get("start")
            end = e.get("end")
            if not start or not end:
                continue
            days = (pd.Timestamp(end) - pd.Timestamp(start)).days
            if not (75 <= days <= 100):
                continue
        end = e["end"]
        existing = by_end.get(end)
        if existing is None or e["filed"] > existing["filed"]:
            by_end[end] = e
    return by_end


def build_statement(cik: str, statement: str, n_periods: int = 4) -> pd.DataFrame:
    """Build a statement as a DataFrame: rows are line items, columns
    are the most recent `n_periods` fiscal quarter-end dates, most
    recent first."""
    if statement not in STATEMENTS:
        raise SecEdgarError(f"Unknown statement '{statement}'. Choose one of {list(STATEMENTS)}.")

    company_facts = get_company_facts(cik)
    line_items = STATEMENTS[statement]

    all_period_ends: set[str] = set()
    row_data: dict[str, dict[str, float]] = {}

    for item in line_items:
        points: dict[str, dict] = {}
        for tag in item.tags:
            entries = _facts_for_tag(company_facts, tag)
            points = _quarterly_points(entries, item.kind)
            if points:
                break  # first tag with data wins
        row_data[item.label] = {end: point["val"] for end, point in points.items()}
        all_period_ends.update(row_data[item.label].keys())

    period_ends = sorted(all_period_ends, reverse=True)[:n_periods]

    df = pd.DataFrame(
        {end: [row_data[item.label].get(end) for item in line_items] for end in period_ends},
        index=[item.label for item in line_items],
    )
    df = df[sorted(df.columns, reverse=True)]  # most recent period first
    return df


def company_name(cik: str) -> str:
    return get_company_facts(cik).get("entityName", "")


if __name__ == "__main__":
    # A quick manual smoke test you can run directly:
    #   python sec_edgar.py META
    import sys

    ticker = sys.argv[1] if len(sys.argv) > 1 else "META"
    cik = get_cik_for_ticker(ticker)
    print(f"{ticker} -> CIK {cik} ({company_name(cik)})")
    for stmt in STATEMENTS:
        print(f"\n=== {stmt} ===")
        print(build_statement(cik, stmt))
