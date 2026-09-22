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
#
# `category` groups fields under a subheading in the app's sidebar
# (Tier 2) -- it doesn't affect the data at all, purely display
# organization, e.g. so the Balance Sheet's checkboxes are grouped into
# Assets / Liabilities / Equity instead of one long flat list.

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
    category: str = "Other"
    sign: float = 1.0
    # A handful of cash-flow "IncreaseDecreaseInX" tags for ASSET
    # accounts (receivables, prepaid expenses, other assets) are filed
    # under GAAP's XBRL rules as "the asset balance increased by $Y"
    # (positive Y) -- which is the OPPOSITE sign from how it's printed
    # in the actual cash flow statement, where an asset increase is a
    # USE of cash shown as a negative. Verified directly against Meta's
    # own filed data: "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets"
    # reports +3,230M for H1 2026, but the printed statement shows
    # (3,230). Liability-side tags (AP, accrued liabilities) don't have
    # this quirk -- their raw value already matches what's printed.
    # `sign=-1` flips an asset-side tag's value so what this app shows
    # matches what's actually on the filing.


STATEMENTS: dict[str, list[LineItem]] = {
    "Balance Sheet": [
        LineItem(
            "Cash and cash equivalents",
            ["CashAndCashEquivalentsAtCarryingValue", "Cash"],
            "instant",
            category="Assets",
        ),
        LineItem(
            "Short-term investments",
            ["ShortTermInvestments", "MarketableSecuritiesCurrent"],
            "instant",
            category="Assets",
        ),
        LineItem("Accounts receivable, net", ["AccountsReceivableNetCurrent"], "instant", category="Assets"),
        LineItem("Inventory", ["InventoryNet"], "instant", category="Assets"),
        LineItem(
            "Prepaid expenses & other current assets",
            ["PrepaidExpenseAndOtherAssetsCurrent"],
            "instant",
            category="Assets",
        ),
        LineItem("Total current assets", ["AssetsCurrent"], "instant", category="Assets"),
        LineItem(
            "Property and equipment, net",
            [
                "PropertyPlantAndEquipmentNet",
                # Meta stopped using the plain tag after Q3 2020 in
                # favor of this longer one (which folds in finance
                # lease right-of-use assets) -- verified against Meta's
                # own filed data, where this second tag's value exactly
                # matches the "Property and equipment, net" line for
                # every period from 2021 through Q1 2026. Tesla, by
                # contrast, reports its CURRENT quarter under this same
                # longer tag, so this fixes both companies, even though
                # neither uses the plain tag anymore for recent periods.
                "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
            ],
            "instant",
            category="Assets",
        ),
        LineItem(
            "Operating lease right-of-use assets",
            ["OperatingLeaseRightOfUseAsset"],
            "instant",
            category="Assets",
        ),
        LineItem("Goodwill", ["Goodwill"], "instant", category="Assets"),
        LineItem(
            "Intangible assets, net", ["FiniteLivedIntangibleAssetsNet"], "instant", category="Assets"
        ),
        LineItem("Long-term investments", ["LongTermInvestments"], "instant", category="Assets"),
        LineItem(
            "Deferred tax assets", ["DeferredTaxAssetsNetNoncurrent"], "instant", category="Assets"
        ),
        LineItem("Other non-current assets", ["OtherAssetsNoncurrent"], "instant", category="Assets"),
        LineItem("Total assets", ["Assets"], "instant", category="Assets"),
        LineItem("Accounts payable", ["AccountsPayableCurrent"], "instant", category="Liabilities"),
        LineItem(
            "Accrued and other current liabilities",
            ["AccruedLiabilitiesCurrent"],
            "instant",
            category="Liabilities",
        ),
        LineItem(
            "Operating lease liabilities, current",
            ["OperatingLeaseLiabilityCurrent"],
            "instant",
            category="Liabilities",
        ),
        LineItem(
            "Long-term debt, current portion",
            ["LongTermDebtCurrent"],
            "instant",
            category="Liabilities",
        ),
        LineItem("Total current liabilities", ["LiabilitiesCurrent"], "instant", category="Liabilities"),
        LineItem(
            "Long-term debt",
            ["LongTermDebtNoncurrent", "LongTermDebt"],
            "instant",
            category="Liabilities",
        ),
        LineItem(
            "Operating lease liabilities, non-current",
            ["OperatingLeaseLiabilityNoncurrent"],
            "instant",
            category="Liabilities",
        ),
        LineItem(
            "Deferred tax liabilities",
            ["DeferredTaxLiabilitiesNoncurrent"],
            "instant",
            category="Liabilities",
        ),
        LineItem(
            "Other long-term liabilities",
            ["OtherLiabilitiesNoncurrent"],
            "instant",
            category="Liabilities",
        ),
        LineItem("Total liabilities", ["Liabilities"], "instant", category="Liabilities"),
        LineItem(
            "Common stock & additional paid-in capital",
            ["AdditionalPaidInCapital"],
            "instant",
            category="Equity",
        ),
        LineItem(
            "Accumulated other comprehensive income (loss)",
            ["AccumulatedOtherComprehensiveIncomeLossNetOfTax"],
            "instant",
            category="Equity",
        ),
        LineItem(
            "Retained earnings", ["RetainedEarningsAccumulatedDeficit"], "instant", category="Equity"
        ),
        LineItem("Total stockholders' equity", ["StockholdersEquity"], "instant", category="Equity"),
        LineItem(
            "Total liabilities and equity",
            ["LiabilitiesAndStockholdersEquity"],
            "instant",
            category="Equity",
        ),
    ],
    "Income Statement": [
        LineItem(
            "Total revenue",
            ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"],
            "duration",
            category="Revenue & gross profit",
        ),
        LineItem(
            "Cost of revenue", ["CostOfRevenue"], "duration", category="Revenue & gross profit"
        ),
        LineItem(
            "Gross profit",
            ["GrossProfit"],
            "duration",
            fallback=lambda r: _safe_sub(r.get("Total revenue"), r.get("Cost of revenue")),
            category="Revenue & gross profit",
        ),
        LineItem(
            "Research and development",
            ["ResearchAndDevelopmentExpense"],
            "duration",
            category="Operating expenses",
        ),
        LineItem(
            "Sales and marketing",
            ["SellingAndMarketingExpense"],
            "duration",
            category="Operating expenses",
        ),
        LineItem(
            "General and administrative",
            ["GeneralAndAdministrativeExpense"],
            "duration",
            category="Operating expenses",
        ),
        LineItem(
            "Selling, general and administrative",
            ["SellingGeneralAndAdministrativeExpense"],
            "duration",
            category="Operating expenses",
        ),
        LineItem(
            "Total operating expenses", ["CostsAndExpenses"], "duration", category="Operating expenses"
        ),
        LineItem(
            "Operating income", ["OperatingIncomeLoss"], "duration", category="Operating expenses"
        ),
        LineItem(
            "Interest income",
            ["InvestmentIncomeInterest"],
            "duration",
            category="Non-operating & taxes",
        ),
        LineItem(
            "Interest expense", ["InterestExpense"], "duration", category="Non-operating & taxes"
        ),
        LineItem(
            "Other income (expense), net",
            ["NonoperatingIncomeExpense"],
            "duration",
            category="Non-operating & taxes",
        ),
        LineItem(
            "Income before taxes",
            ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"],
            "duration",
            category="Non-operating & taxes",
        ),
        LineItem(
            "Provision for income taxes",
            ["IncomeTaxExpenseBenefit"],
            "duration",
            category="Non-operating & taxes",
        ),
        LineItem("Net income", ["NetIncomeLoss"], "duration", category="Non-operating & taxes"),
        LineItem(
            "EPS, basic",
            ["EarningsPerShareBasic"],
            "duration",
            unit="USD/shares",
            category="Per share",
        ),
        LineItem(
            "EPS, diluted",
            ["EarningsPerShareDiluted"],
            "duration",
            unit="USD/shares",
            category="Per share",
        ),
        LineItem(
            "Weighted avg. shares, basic",
            ["WeightedAverageNumberOfSharesOutstandingBasic"],
            "duration",
            unit="shares",
            category="Per share",
        ),
        LineItem(
            "Weighted avg. shares, diluted",
            ["WeightedAverageNumberOfDilutedSharesOutstanding"],
            "duration",
            unit="shares",
            category="Per share",
        ),
    ],
    "Cash Flow Statement": [
        LineItem(
            "Depreciation and amortization",
            ["DepreciationDepletionAndAmortization"],
            "duration",
            category="Operating activities",
        ),
        LineItem(
            "Stock-based compensation",
            ["ShareBasedCompensation"],
            "duration",
            category="Operating activities",
        ),
        LineItem(
            "Deferred income taxes",
            ["DeferredIncomeTaxExpenseBenefit"],
            "duration",
            category="Operating activities",
        ),
        # Working-capital changes -- the "Changes in assets and
        # liabilities" section of the cash flow statement. Each of
        # these tags is verified against Meta's own filed H1-2026 data.
        # ASSET-side tags (receivable, prepaid, other assets) need
        # sign=-1 -- see the note on LineItem.sign above for why.
        # LIABILITY-side tags (payable, accrued, other liabilities)
        # don't need flipping; their raw value already matches what's
        # printed on the statement.
        LineItem(
            "Change in accounts receivable",
            ["IncreaseDecreaseInAccountsReceivable", "IncreaseDecreaseInAccountsReceivableTrade"],
            "duration",
            category="Working capital changes",
            sign=-1,
        ),
        LineItem(
            "Change in prepaid expenses & other current assets",
            ["IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets", "IncreaseDecreaseInPrepaidExpense"],
            "duration",
            category="Working capital changes",
            sign=-1,
        ),
        LineItem(
            "Change in other assets",
            ["IncreaseDecreaseInOtherOperatingAssets", "IncreaseDecreaseInOtherAssets"],
            "duration",
            category="Working capital changes",
            sign=-1,
        ),
        LineItem(
            "Change in accounts payable",
            ["IncreaseDecreaseInAccountsPayableTrade", "IncreaseDecreaseInAccountsPayable"],
            "duration",
            category="Working capital changes",
        ),
        LineItem(
            "Change in accrued expenses & other current liabilities",
            ["IncreaseDecreaseInAccruedLiabilities", "IncreaseDecreaseInAccruedLiabilitiesCurrent"],
            "duration",
            category="Working capital changes",
        ),
        LineItem(
            "Change in other liabilities",
            ["IncreaseDecreaseInOtherOperatingLiabilities", "IncreaseDecreaseInOtherLiabilities"],
            "duration",
            category="Working capital changes",
        ),
        LineItem(
            "Net cash from operating activities",
            ["NetCashProvidedByUsedInOperatingActivities"],
            "duration",
            category="Operating activities",
        ),
        LineItem(
            "Capital expenditures",
            ["PaymentsToAcquirePropertyPlantAndEquipment"],
            "duration",
            category="Investing activities",
        ),
        LineItem(
            "Purchases of investments",
            [
                "PaymentsToAcquireInvestments",
                # Meta switched to this tag some time after 2022 (the
                # plain tag above has no data for them past that point)
                # -- verified against Meta's own filed Q1-2026 data,
                # where this tag reports exactly $32,978M.
                "PaymentsToAcquireAvailableForSaleSecuritiesDebt",
            ],
            "duration",
            category="Investing activities",
        ),
        LineItem(
            "Maturities/sales of investments",
            [
                "ProceedsFromSaleMaturityAndCollectionsOfInvestments",
                "ProceedsFromSaleAndMaturityOfMarketableSecurities",
            ],
            "duration",
            category="Investing activities",
        ),
        LineItem(
            "Acquisitions, net of cash acquired",
            ["PaymentsToAcquireBusinessesNetOfCashAcquired"],
            "duration",
            category="Investing activities",
        ),
        LineItem(
            "Net cash from investing activities",
            ["NetCashProvidedByUsedInInvestingActivities"],
            "duration",
            category="Investing activities",
        ),
        LineItem(
            "Repurchases of common stock",
            ["PaymentsForRepurchaseOfCommonStock"],
            "duration",
            category="Financing activities",
        ),
        LineItem(
            "Proceeds from debt issuance",
            ["ProceedsFromIssuanceOfLongTermDebt"],
            "duration",
            category="Financing activities",
        ),
        LineItem(
            "Repayments of debt",
            ["RepaymentsOfLongTermDebt"],
            "duration",
            category="Financing activities",
        ),
        LineItem(
            "Net cash from financing activities",
            ["NetCashProvidedByUsedInFinancingActivities"],
            "duration",
            category="Financing activities",
        ),
        LineItem(
            "Effect of exchange rate changes",
            ["EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
            "duration",
            category="Other",
        ),
        LineItem(
            "Net change in cash",
            [
                "CashAndCashEquivalentsPeriodIncreaseDecrease",
                "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
            ],
            "duration",
            category="Other",
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
    category: str = "Other"


DERIVED_METRICS: dict[str, list[DerivedMetric]] = {
    "Income Statement": [
        DerivedMetric(
            "Gross margin %",
            lambda r: _safe_div(r.get("Gross profit"), r.get("Total revenue")),
            fmt="pct",
            category="Revenue & gross profit",
        ),
        DerivedMetric(
            "Operating margin %",
            lambda r: _safe_div(r.get("Operating income"), r.get("Total revenue")),
            fmt="pct",
            category="Operating expenses",
        ),
        DerivedMetric(
            "Net margin %",
            lambda r: _safe_div(r.get("Net income"), r.get("Total revenue")),
            fmt="pct",
            category="Non-operating & taxes",
        ),
    ],
    "Cash Flow Statement": [
        DerivedMetric(
            "Free cash flow",
            lambda r: _safe_sub(r.get("Net cash from operating activities"), r.get("Capital expenditures")),
            category="Operating activities",
        ),
        DerivedMetric(
            "Change in working capital",
            lambda r: _safe_add(
                r.get("Change in accounts receivable"),
                r.get("Change in prepaid expenses & other current assets"),
                r.get("Change in other assets"),
                r.get("Change in accounts payable"),
                r.get("Change in accrued expenses & other current liabilities"),
                r.get("Change in other liabilities"),
            ),
            category="Working capital changes",
        ),
    ],
    "Balance Sheet": [
        DerivedMetric(
            "Total debt",
            lambda r: _safe_add(r.get("Long-term debt"), r.get("Long-term debt, current portion")),
            category="Leverage & liquidity",
        ),
        DerivedMetric(
            "LT (non-current) liabilities",
            lambda r: _safe_sub(r.get("Total liabilities"), r.get("Total current liabilities")),
            category="Leverage & liquidity",
        ),
        DerivedMetric(
            # Depends on "Total debt" above -- see the note in
            # build_statement() about why list ORDER matters here.
            "Debt / equity",
            lambda r: _safe_div(r.get("Total debt"), r.get("Total stockholders' equity")),
            fmt="ratio",
            category="Leverage & liquidity",
        ),
        DerivedMetric(
            "Current ratio",
            lambda r: _safe_div(r.get("Total current assets"), r.get("Total current liabilities")),
            fmt="ratio",
            category="Leverage & liquidity",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Field toggle & preset system (Tier 2)
# ---------------------------------------------------------------------------
# A "preset" is just a named set of field labels, drawn from ANY
# statement -- e.g. "Concise" mixes in Total revenue (Income Statement),
# Total assets (Balance Sheet), and Free cash flow (Cash Flow
# Statement) all in one preset. app.py intersects a preset's fields
# with whichever single statement is on screen (see
# preset_default_fields()) to decide that statement's default
# checkboxes; the preset itself doesn't know or care which statement
# each field belongs to.

@dataclass
class Preset:
    label: str
    fields: set[str]


def statement_field_labels(statement: str) -> list[str]:
    """Every label (raw line items, then derived metrics) that
    build_statement(statement, ...) can produce, in the same order
    build_statement uses. Doesn't touch the network -- these come
    straight from STATEMENTS/DERIVED_METRICS, so the sidebar can show
    toggles before any data has been fetched."""
    items = STATEMENTS.get(statement, [])
    metrics = DERIVED_METRICS.get(statement, [])
    return [item.label for item in items] + [m.label for m in metrics]


def statement_categories(statement: str) -> dict[str, list[str]]:
    """Groups a statement's field labels by their `category`, preserving
    each category's first-appearance order (so 'Assets' comes before
    'Liabilities' for the Balance Sheet, etc.) -- what the sidebar loops
    over to draw one subheading per category."""
    groups: dict[str, list[str]] = {}
    for item in STATEMENTS.get(statement, []):
        groups.setdefault(item.category, []).append(item.label)
    for metric in DERIVED_METRICS.get(statement, []):
        groups.setdefault(metric.category, []).append(metric.label)
    return groups


def _all_field_labels() -> set[str]:
    """Every label build_statement() can produce, across every
    statement -- what the "Full" preset means."""
    labels: set[str] = set()
    for statement in STATEMENTS:
        labels.update(statement_field_labels(statement))
    return labels


PRESETS: list[Preset] = [
    Preset(
        "Concise",
        {
            "Total revenue",
            "Net income",
            "Total assets",
            "Total debt",
            "Total stockholders' equity",
            "Net cash from operating activities",
            "Free cash flow",
            "Cash and cash equivalents",
            "Capital expenditures",
            "Depreciation and amortization",
            "Interest expense",
            "Operating income",
            "Total operating expenses",
        },
    ),
    Preset("Full", _all_field_labels()),
    Preset(
        "Free Cash Flow Mode",
        {
            "Total revenue",
            "Net income",
            "Net cash from operating activities",
            "Capital expenditures",
            "Free cash flow",
        },
    ),
    Preset(
        "Profitability Mode",
        {
            "Total revenue",
            "Gross profit",
            "Gross margin %",
            "Operating income",
            "Operating margin %",
            "Net income",
            "Net margin %",
            "EPS, diluted",
        },
    ),
    Preset(
        "Leverage Mode",
        {
            "Total assets",
            "Total liabilities",
            "Total debt",
            "Total stockholders' equity",
            "Debt / equity",
            "Cash and cash equivalents",
        },
    ),
]

PRESETS_BY_LABEL: dict[str, Preset] = {p.label: p for p in PRESETS}


def preset_default_fields(preset_label: str, statement: str) -> set[str]:
    """Which of `statement`'s own fields the given preset turns on by
    default -- the preset's full field set (which spans every
    statement) intersected with what this one statement can actually
    show. An unknown preset label falls back to "everything on" rather
    than "everything off", so a typo never silently hides the whole
    statement."""
    preset = PRESETS_BY_LABEL.get(preset_label)
    all_fields = set(statement_field_labels(statement))
    if preset is None:
        return all_fields
    return preset.fields & all_fields


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
    by preferring the most-recently-filed one).

    Some cash-flow-statement line items are only ever tagged
    cumulatively -- six months, nine months, a full year -- and NEVER
    as a standalone quarter. This is legitimate under SEC rules (a
    10-Q's cash flow statement is allowed to be presented
    year-to-date only, unlike the income statement, which usually also
    breaks out the discrete quarter), and it's common for line items
    that aren't a routine, every-quarter event -- verified directly
    against Meta's own filed data for "proceeds from issuance of
    long-term debt," which only ever appears as a 6-month, 9-month, or
    full-year cumulative total, never a standalone quarter. Below,
    after collecting genuine standalone-quarter entries the normal
    way, a second pass derives a standalone quarter for any period end
    that ONLY has a cumulative entry, the same way an analyst reading
    the filing by hand would: this period's cumulative total minus the
    previous period's cumulative total, walked forward one fiscal year
    at a time. A period end that never gets an earlier cumulative
    point to subtract from (e.g. no Q1 was ever reported) is left
    unfilled rather than guessed at."""
    by_end: dict[str, dict] = {}
    # For "duration" facts, every entry sharing the same `start` date
    # belongs to the same fiscal-year cumulative chain (regardless of
    # what that start date's actual month/day is, so this works for
    # non-calendar fiscal years too) -- collected here so the second
    # pass below can walk each chain from earliest to latest.
    by_start: dict[str, dict[str, dict]] = {}

    for e in entries:
        if e.get("form") not in ("10-Q", "10-K"):
            continue
        if kind == "duration":
            start = e.get("start")
            end = e.get("end")
            if not start or not end:
                continue
            days = (pd.Timestamp(end) - pd.Timestamp(start)).days

            chain = by_start.setdefault(start, {})
            existing_chain_entry = chain.get(end)
            if existing_chain_entry is None or e["filed"] > existing_chain_entry["filed"]:
                chain[end] = e

            if not (75 <= days <= 100):
                # Not a standalone quarter -- it may still be useful as
                # a cumulative point in the second pass below, but it
                # doesn't go directly into by_end.
                continue

        end = e["end"]
        existing = by_end.get(end)
        if existing is None or e["filed"] > existing["filed"]:
            by_end[end] = e

    if kind == "duration":
        for start, chain in by_start.items():
            ordered_ends = sorted(chain, key=lambda end: pd.Timestamp(end))
            prev_cumulative_val = None
            for end in ordered_ends:
                if end in by_end:
                    # Already have a genuine standalone-quarter value
                    # here (e.g. Q1's ~90-day entry IS the cumulative
                    # total so far, since it's the first quarter) --
                    # never overwrite a real reported value, but DO use
                    # it as the next subtraction's baseline.
                    prev_cumulative_val = by_end[end]["val"]
                    continue
                entry = chain[end]
                if prev_cumulative_val is not None:
                    by_end[end] = {
                        **entry,
                        "val": entry["val"] - prev_cumulative_val,
                        "derived": True,
                    }
                # Advance the baseline to this period's cumulative
                # total regardless of whether we could derive a
                # standalone value for it -- a later period in the same
                # chain (e.g. the full year, once nine months is known)
                # can still be derived from it even if this one couldn't.
                prev_cumulative_val = entry["val"]

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
        row_data[item.label] = {end: point["val"] * item.sign for end, point in points.items()}
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
