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
from typing import Callable

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
# -- and the SAME company can even switch tags partway through its
# filing history (Meta tagged revenue as "Revenues" through 2018, then
# switched to "RevenueFromContractWithCustomerExcludingAssessedTax").
# For each line item we list candidate tags in priority order; for any
# given period end, the highest-priority tag that actually reported
# that period wins, and a lower-priority tag only fills in period ends
# no earlier tag covered. See the merge loop in build_statement().
#
# "instant" tags are balance-sheet-style: a snapshot at one date.
# "duration" tags are income/cash-flow-style: a total over a period
# (e.g. the quarter from 2026-04-01 to 2026-06-30).

@dataclass
class LineItem:
    label: str
    tags: list[str]
    kind: str  # "instant" or "duration"
    unit: str = "USD"  # most tags report in USD, but EPS is "USD/shares"
    # and share counts are "shares" -- SEC files each fact under a unit
    # key matching what it actually measures.
    fallback: Callable[[dict[str, float | None]], float | None] | None = None
    # Some companies just don't report every line item as its own XBRL
    # tag (some don't break out Gross profit at all). When every
    # candidate tag comes up empty for a period, `fallback` -- if set --
    # gets a chance to compute the value from OTHER line items already
    # known for that same period (e.g. Total revenue - Cost of revenue),
    # the same way a DerivedMetric formula works below. Unlike a
    # DerivedMetric, a fallback fills gaps in a REAL line item -- it
    # only runs where the tags found nothing, never overriding an
    # actually-reported value.


STATEMENTS: dict[str, list[LineItem]] = {
    "Balance Sheet": [
        LineItem("Cash and cash equivalents", ["CashAndCashEquivalentsAtCarryingValue", "Cash"], "instant"),
        LineItem("Short-term investments", ["ShortTermInvestments", "MarketableSecuritiesCurrent"], "instant"),
        LineItem("Accounts receivable, net", ["AccountsReceivableNetCurrent"], "instant"),
        LineItem("Inventory", ["InventoryNet"], "instant"),
        LineItem("Prepaid expenses & other current assets", ["PrepaidExpenseAndOtherAssetsCurrent"], "instant"),
        LineItem("Total current assets", ["AssetsCurrent"], "instant"),
        LineItem("Property and equipment, net", ["PropertyPlantAndEquipmentNet"], "instant"),
        LineItem("Operating lease right-of-use assets", ["OperatingLeaseRightOfUseAsset"], "instant"),
        LineItem("Goodwill", ["Goodwill"], "instant"),
        LineItem("Intangible assets, net", ["FiniteLivedIntangibleAssetsNet"], "instant"),
        LineItem("Long-term investments", ["LongTermInvestments"], "instant"),
        LineItem("Deferred tax assets", ["DeferredTaxAssetsNetNoncurrent"], "instant"),
        LineItem("Other non-current assets", ["OtherAssetsNoncurrent"], "instant"),
        LineItem("Total assets", ["Assets"], "instant"),
        LineItem("Accounts payable", ["AccountsPayableCurrent"], "instant"),
        LineItem("Accrued and other current liabilities", ["AccruedLiabilitiesCurrent"], "instant"),
        LineItem("Operating lease liabilities, current", ["OperatingLeaseLiabilityCurrent"], "instant"),
        LineItem("Long-term debt, current portion", ["LongTermDebtCurrent"], "instant"),
        LineItem("Total current liabilities", ["LiabilitiesCurrent"], "instant"),
        LineItem("Long-term debt", ["LongTermDebtNoncurrent", "LongTermDebt"], "instant"),
        LineItem("Operating lease liabilities, non-current", ["OperatingLeaseLiabilityNoncurrent"], "instant"),
        LineItem("Deferred tax liabilities", ["DeferredTaxLiabilitiesNoncurrent"], "instant"),
        LineItem("Other long-term liabilities", ["OtherLiabilitiesNoncurrent"], "instant"),
        LineItem("Total liabilities", ["Liabilities"], "instant"),
        LineItem("Common stock & additional paid-in capital", ["AdditionalPaidInCapital"], "instant"),
        LineItem(
            "Accumulated other comprehensive income (loss)",
            ["AccumulatedOtherComprehensiveIncomeLossNetOfTax"],
            "instant",
        ),
        LineItem("Retained earnings", ["RetainedEarningsAccumulatedDeficit"], "instant"),
        LineItem("Total stockholders' equity", ["StockholdersEquity"], "instant"),
        LineItem("Total liabilities and equity", ["LiabilitiesAndStockholdersEquity"], "instant"),
    ],
    "Income Statement": [
        LineItem("Total revenue", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"], "duration"),
        LineItem("Cost of revenue", ["CostOfRevenue"], "duration"),
        LineItem(
            "Gross profit",
            ["GrossProfit"],
            "duration",
            fallback=lambda r: _safe_sub(r.get("Total revenue"), r.get("Cost of revenue")),
        ),
        LineItem("Research and development", ["ResearchAndDevelopmentExpense"], "duration"),
        LineItem("Sales and marketing", ["SellingAndMarketingExpense"], "duration"),
        LineItem("General and administrative", ["GeneralAndAdministrativeExpense"], "duration"),
        LineItem(
            "Selling, general and administrative",
            ["SellingGeneralAndAdministrativeExpense"],
            "duration",
        ),
        LineItem("Total operating expenses", ["CostsAndExpenses"], "duration"),
        LineItem("Operating income", ["OperatingIncomeLoss"], "duration"),
        LineItem("Interest income", ["InvestmentIncomeInterest"], "duration"),
        LineItem("Interest expense", ["InterestExpense"], "duration"),
        LineItem("Other income (expense), net", ["NonoperatingIncomeExpense"], "duration"),
        LineItem(
            "Income before taxes",
            ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"],
            "duration",
        ),
        LineItem("Provision for income taxes", ["IncomeTaxExpenseBenefit"], "duration"),
        LineItem("Net income", ["NetIncomeLoss"], "duration"),
        LineItem("EPS, basic", ["EarningsPerShareBasic"], "duration", unit="USD/shares"),
        LineItem("EPS, diluted", ["EarningsPerShareDiluted"], "duration", unit="USD/shares"),
        LineItem(
            "Weighted avg. shares, basic",
            ["WeightedAverageNumberOfSharesOutstandingBasic"],
            "duration",
            unit="shares",
        ),
        LineItem(
            "Weighted avg. shares, diluted",
            ["WeightedAverageNumberOfDilutedSharesOutstanding"],
            "duration",
            unit="shares",
        ),
    ],
    "Cash Flow Statement": [
        LineItem("Depreciation and amortization", ["DepreciationDepletionAndAmortization"], "duration"),
        LineItem("Stock-based compensation", ["ShareBasedCompensation"], "duration"),
        LineItem("Deferred income taxes", ["DeferredIncomeTaxExpenseBenefit"], "duration"),
        LineItem("Net cash from operating activities", ["NetCashProvidedByUsedInOperatingActivities"], "duration"),
        LineItem("Capital expenditures", ["PaymentsToAcquirePropertyPlantAndEquipment"], "duration"),
        LineItem("Purchases of investments", ["PaymentsToAcquireInvestments"], "duration"),
        LineItem(
            "Maturities/sales of investments",
            ["ProceedsFromSaleMaturityAndCollectionsOfInvestments"],
            "duration",
        ),
        LineItem(
            "Acquisitions, net of cash acquired",
            ["PaymentsToAcquireBusinessesNetOfCashAcquired"],
            "duration",
        ),
        LineItem("Net cash from investing activities", ["NetCashProvidedByUsedInInvestingActivities"], "duration"),
        LineItem("Repurchases of common stock", ["PaymentsForRepurchaseOfCommonStock"], "duration"),
        LineItem("Proceeds from debt issuance", ["ProceedsFromIssuanceOfLongTermDebt"], "duration"),
        LineItem("Repayments of debt", ["RepaymentsOfLongTermDebt"], "duration"),
        LineItem("Net cash from financing activities", ["NetCashProvidedByUsedInFinancingActivities"], "duration"),
        LineItem(
            "Effect of exchange rate changes",
            ["EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
            "duration",
        ),
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
# Derived metrics -- computed from two or more of the raw fields above,
# rather than pulled directly from a single XBRL tag.
# ---------------------------------------------------------------------------
# Each formula receives one dict, `row_values`, mapping every OTHER
# label already known for that same period (raw fields above it, plus
# any derived metric earlier in this same statement's list) to its
# number -- or None if that field had no data for this period. Formulas
# use `row_values.get(...)`, never `row_values[...]`, so a missing
# input quietly produces a missing result (None) instead of crashing
# the whole page.


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    """a / b, but None (not a crash, not a fake 0) whenever either side
    is missing or the denominator is zero."""
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _safe_add(*values: float | None) -> float | None:
    """Sum of the given values, or None if every single one is missing."""
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _safe_sub(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return a - b


@dataclass
class DerivedMetric:
    label: str
    formula: Callable[[dict[str, float | None]], float | None]
    fmt: str = "dollar"  # "dollar" | "pct" | "ratio" -- how app.py should display it


DERIVED_METRICS: dict[str, list[DerivedMetric]] = {
    "Income Statement": [
        DerivedMetric(
            "Gross margin %",
            lambda r: _safe_div(r.get("Gross profit"), r.get("Total revenue")),
            fmt="pct",
        ),
        DerivedMetric(
            "Operating margin %",
            lambda r: _safe_div(r.get("Operating income"), r.get("Total revenue")),
            fmt="pct",
        ),
        DerivedMetric(
            "Net margin %",
            lambda r: _safe_div(r.get("Net income"), r.get("Total revenue")),
            fmt="pct",
        ),
    ],
    "Cash Flow Statement": [
        DerivedMetric(
            "Free cash flow",
            lambda r: _safe_sub(r.get("Net cash from operating activities"), r.get("Capital expenditures")),
        ),
    ],
    "Balance Sheet": [
        DerivedMetric(
            "Total debt",
            lambda r: _safe_add(r.get("Long-term debt"), r.get("Long-term debt, current portion")),
        ),
        DerivedMetric(
            "LT (non-current) liabilities",
            lambda r: _safe_sub(r.get("Total liabilities"), r.get("Total current liabilities")),
        ),
        DerivedMetric(
            # Depends on "Total debt" above -- see the note in
            # build_statement() about why list ORDER matters here.
            "Debt / equity",
            lambda r: _safe_div(r.get("Total debt"), r.get("Total stockholders' equity")),
            fmt="ratio",
        ),
        DerivedMetric(
            "Current ratio",
            lambda r: _safe_div(r.get("Total current assets"), r.get("Total current liabilities")),
            fmt="ratio",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Building a statement DataFrame
# ---------------------------------------------------------------------------

def _facts_for_tag(company_facts: dict, tag: str, unit: str = "USD") -> list[dict]:
    """Pull the raw list of reported values for one us-gaap tag out of a
    company-facts blob, under the given unit (most line items are
    "USD"; EPS is "USD/shares", share counts are "shares"). Returns []
    if the company never used this tag, or never reported it in that
    unit."""
    try:
        return company_facts["facts"]["us-gaap"][tag]["units"][unit]
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
    """Build a statement as a DataFrame: rows are line items (raw fields
    from STATEMENTS, followed by that statement's DERIVED_METRICS),
    columns are the most recent `n_periods` fiscal quarter-end dates,
    most recent first."""
    if statement not in STATEMENTS:
        raise SecEdgarError(f"Unknown statement '{statement}'. Choose one of {list(STATEMENTS)}.")

    company_facts = get_company_facts(cik)
    line_items = STATEMENTS[statement]
    derived_metrics = DERIVED_METRICS.get(statement, [])

    all_period_ends: set[str] = set()
    row_data: dict[str, dict[str, float]] = {}

    for item in line_items:
        # Merge points from every candidate tag, in priority order --
        # NOT "first tag with any data wins". A company can retire a tag
        # partway through its filing history (Meta reported revenue as
        # "Revenues" only through 2018, then switched to
        # "RevenueFromContractWithCustomerExcludingAssessedTax"), so
        # "Revenues" alone still has SOME data, just none recent. If we
        # stopped at the first tag with any data at all, recent quarters
        # would come back blank even though a later-listed tag has them.
        # Merging means: for a given period end, the highest-priority
        # tag that actually reported it wins; a lower-priority tag only
        # fills in a period end no earlier tag covered.
        points: dict[str, dict] = {}
        for tag in item.tags:
            entries = _facts_for_tag(company_facts, tag, unit=item.unit)
            tag_points = _quarterly_points(entries, item.kind)
            for end, point in tag_points.items():
                points.setdefault(end, point)
        row_data[item.label] = {end: point["val"] for end, point in points.items()}
        all_period_ends.update(row_data[item.label].keys())

    period_ends = sorted(all_period_ends, reverse=True)[:n_periods]

    # Fill gaps in raw line items that went un-reported for a period,
    # using each item's `fallback` (if it has one) -- e.g. a company
    # that doesn't break out "Gross profit" as its own XBRL tag still
    # lets us show Total revenue - Cost of revenue. This only fills a
    # period that came back completely empty; a real reported value is
    # never overwritten.
    for item in line_items:
        if item.fallback is None:
            continue
        values = row_data[item.label]
        for end in period_ends:
            if values.get(end) is not None:
                continue
            period_values = {label: vals.get(end) for label, vals in row_data.items() if label != item.label}
            computed = item.fallback(period_values)
            if computed is not None:
                values[end] = computed

    # Derived metrics run AFTER every raw field is known, one metric at
    # a time, one period at a time -- a formula like "gross margin"
    # needs that period's Gross profit and Total revenue to already be
    # sitting in row_data. Metrics run in DERIVED_METRICS list order,
    # so a metric that depends on an earlier metric (Debt/equity needs
    # Total debt, both under "Balance Sheet" above) works correctly
    # because Total debt is fully computed, for every period, before
    # Debt/equity's turn starts.
    for metric in derived_metrics:
        row_data[metric.label] = {}
        for end in period_ends:
            period_values = {label: values.get(end) for label, values in row_data.items() if label != metric.label}
            row_data[metric.label][end] = metric.formula(period_values)

    all_labels = [item.label for item in line_items] + [m.label for m in derived_metrics]

    df = pd.DataFrame(
        {end: [row_data[label].get(end) for label in all_labels] for end in period_ends},
        index=all_labels,
    )
    df = df[sorted(df.columns, reverse=True)]  # most recent period first
    return df


def field_formats(statement: str) -> dict[str, str]:
    """Maps every row label build_statement(statement, ...) can produce
    to a display-format hint ('dollar', 'pct', or 'ratio'). app.py uses
    this so a margin doesn't get rounded down to '0' like a dollar
    figure would."""
    formats = {item.label: "dollar" for item in STATEMENTS.get(statement, [])}
    formats.update({metric.label: metric.fmt for metric in DERIVED_METRICS.get(statement, [])})
    return formats


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
