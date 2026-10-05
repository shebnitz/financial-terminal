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

One thing SEC EDGAR does NOT have: live market data. A company's filed
financial statements say nothing about what its stock is trading at
right now, so the Comparables Analysis section near the bottom of this
file (see build_comparables_table()) pulls Price / Market Cap / Beta /
P-B / P-S from Finviz instead, via the free `finvizfinance` package --
an unofficial library that reads finviz.com's own quote pages, not a
documented API. That's a real trade-off worth knowing: unlike SEC's
API, Finviz doesn't publish a stable contract, so a page redesign on
their end can break this at any time with no warning. Charter Section 5
covers this decision.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable

import pandas as pd
import requests
import finvizfinance.quote as _finviz_quote_module
from finvizfinance.quote import finvizfinance as _FinvizQuote
from finvizfinance.util import number_convert as _finviz_number_convert

import config

# --- Patch a real bug in finvizfinance 1.5.0's own number_convert() ---
# As of late Sept 2026, finviz.com started returning some fundament
# fields as whitespace-only text (e.g. a non-breaking space) instead
# of an actual value or a "-" placeholder. finvizfinance's own
# number_convert() only special-cases a TRULY empty string or a literal
# "-" (checked BEFORE it strips whitespace), so a whitespace-only value
# slips past that check, gets stripped down to "", and then a bare
# `num[-1]` blows up with "IndexError: string index out of range" --
# an error that isn't even caught by finvizfinance's own `except
# ValueError`, so it propagates all the way up and (via our own broad
# except in get_market_data() below) shows up as "Couldn't get Finviz
# market data for '<TICKER>': string index out of range" for every
# single ticker, every time -- exactly the kind of "Finviz redesigned
# their page and broke this overnight" failure the module docstring
# above warns about.
#
# There's no reasonable upstream fix to wait on, so we patch it
# ourselves: a safe wrapper that treats a whitespace-only (or "-")
# value as "no data" (None) instead of crashing, then defers to
# finvizfinance's real logic for everything else. quote.py did
# `from finvizfinance.util import number_convert`, which copies the
# NAME into quote.py's own module namespace -- so patching
# finvizfinance.util.number_convert alone wouldn't affect the copy
# quote.py already looked up. We have to reassign the name where
# quote.py actually reads it from: finvizfinance.quote.number_convert.


def _safe_finviz_number_convert(num: str) -> float | None:
    if num is None:
        return None
    stripped = num.strip()
    if not stripped or stripped == "-":
        return None
    return _finviz_number_convert(stripped)


_finviz_quote_module.number_convert = _safe_finviz_number_convert

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL_TMPL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"

HEADERS = {"User-Agent": config.USER_AGENT}

MAX_COMPARABLES_TICKERS = 10


class SecEdgarError(Exception):
    """Raised when we can't get a clean answer back from SEC EDGAR."""


class MarketDataError(Exception):
    """Raised when we can't get a clean answer back from Finviz -- kept
    as its own exception type (rather than reusing SecEdgarError) so
    build_comparables_table() can tell "SEC has no data for this
    ticker" apart from "Finviz has no market data for this ticker,"
    which are different problems with different likely fixes."""


# ---------------------------------------------------------------------------
# Low-level plumbing: cached HTTP GETs
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> str:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    safe_key = key.replace("/", "_").replace(":", "_")
    return os.path.join(config.CACHE_DIR, f"{safe_key}.json")


# Tier 4 (Polish & Robustness): retry a transient failure -- a network
# hiccup, SEC's own servers briefly struggling (5xx), or SEC asking us
# to slow down (429) -- instead of either crashing the whole app with a
# raw requests exception, or giving up on the very first blip. Three
# tries total, waiting longer between each (2s, then 4s): enough to
# ride out a momentary problem without making a real outage feel like
# the app hung.
_MAX_HTTP_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 2.0


def _get_json(url: str, cache_key: str) -> dict:
    """GET a URL as JSON, using an on-disk cache so repeated Streamlit
    reruns don't hammer SEC's servers (and so the app feels instant on
    the second load). Retries a transient failure (network error, SEC
    5xx, SEC 429 rate-limiting) with backoff before giving up -- see
    _MAX_HTTP_ATTEMPTS above -- and every failure that reaches the
    caller is a plain-English SecEdgarError, never a raw requests
    exception."""
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

    last_error: str = ""
    for attempt in range(_MAX_HTTP_ATTEMPTS):
        is_last_attempt = attempt == _MAX_HTTP_ATTEMPTS - 1

        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
        except requests.exceptions.RequestException as e:
            # No connection, DNS failure, timeout, ... -- not something
            # a different ticker or a code fix can help with, and not
            # something a different status code check below can catch
            # either (there was no response at all). Retry like any
            # other transient failure.
            last_error = str(e)
            if not is_last_attempt:
                time.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                continue
            raise SecEdgarError(
                f"Couldn't reach SEC EDGAR after {_MAX_HTTP_ATTEMPTS} attempts -- this usually "
                f"means a network problem on this machine, or SEC's own site being down. "
                f"Last error: {last_error}. Try again in a moment."
            ) from e

        if resp.status_code == 404:
            raise SecEdgarError(f"SEC EDGAR returned 404 for {url}. Double check the ticker/CIK.")
        if resp.status_code == 403:
            raise SecEdgarError(
                "SEC EDGAR returned 403 (forbidden). This almost always means the "
                "User-Agent header was rejected -- check EDGAR_CONTACT_EMAIL in "
                "config.py / .env."
            )
        if resp.status_code == 429:
            # SEC's fair-access guidance asks for no more than ~10
            # requests/second; the disk cache normally keeps this app
            # well under that, but a burst of brand-new lookups (e.g. a
            # fresh 10-ticker Comparables Analysis run) could still trip
            # it. Respect a Retry-After header if SEC sends one, since
            # that's SEC telling us exactly how long to wait.
            if not is_last_attempt:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else _BACKOFF_BASE_SECONDS * (2**attempt)
                time.sleep(delay)
                continue
            raise SecEdgarError(
                f"SEC EDGAR is rate-limiting this app (HTTP 429), even after "
                f"{_MAX_HTTP_ATTEMPTS} attempts with backoff. SEC asks for no more than "
                f"~10 requests/second -- wait a minute and try again."
            )
        if resp.status_code >= 500:
            # A server-side problem at SEC, not something a retry of
            # THIS app's own logic would fix -- but a genuinely
            # transient 5xx (SEC deploying, briefly overloaded) often
            # clears up within a few seconds.
            last_error = f"HTTP {resp.status_code}"
            if not is_last_attempt:
                time.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                continue
            raise SecEdgarError(
                f"SEC EDGAR returned a server error (HTTP {resp.status_code}) after "
                f"{_MAX_HTTP_ATTEMPTS} attempts -- this means SEC's own systems are having "
                f"trouble, not a bug in this app. Try again shortly."
            )
        resp.raise_for_status()  # anything else unexpected -- surfaces as a normal requests error

        data = resp.json()
        with open(path, "w") as f:
            json.dump(data, f)
        return data

    # Unreachable in practice (every branch above either returns or
    # raises), but keeps this function's return type honest.
    raise SecEdgarError(f"Couldn't reach SEC EDGAR: {last_error}")


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


def _quarterly_points(entries: list[dict], kind: str, period_type: str = "quarterly") -> dict[str, dict]:
    """Reduce raw fact entries down to one value per fiscal PERIOD, keyed
    by period end date, keeping only 10-Q / 10-K filed values (skips
    restated/duplicate values from other filings when possible by
    preferring the most-recently-filed one).

    `period_type` (Tier 4): "quarterly" (the default -- one value per
    fiscal quarter, ~90 days) or "annual" (one value per fiscal YEAR,
    ~365 days, from 10-K filings only). Quarterly is everything this
    function originally did; annual is a separate, simpler pass -- see
    below.

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
    unfilled rather than guessed at. This derivation is a QUARTERLY
    concept only -- in annual mode the full-year total is already
    exactly what we want, directly reported, so there's nothing to
    derive."""
    annual = period_type == "annual"
    # Annual totals are what a 10-K itself reports -- a 10-Q would only
    # carry one in an unusual restated/trailing disclosure, so annual
    # mode requires form=="10-K" specifically rather than accepting
    # either filing type the way quarterly mode does.
    allowed_forms = ("10-K",) if annual else ("10-Q", "10-K")

    by_end: dict[str, dict] = {}
    # For "duration" facts, every entry sharing the same `start` date
    # belongs to the same fiscal-year cumulative chain (regardless of
    # what that start date's actual month/day is, so this works for
    # non-calendar fiscal years too) -- collected here so the second
    # pass below can walk each chain from earliest to latest. Only
    # needed in quarterly mode (see the docstring above).
    by_start: dict[str, dict[str, dict]] = {}

    for e in entries:
        if e.get("form") not in allowed_forms:
            continue
        if kind == "duration":
            start = e.get("start")
            end = e.get("end")
            if not start or not end:
                continue
            days = (pd.Timestamp(end) - pd.Timestamp(start)).days

            if annual:
                if not (350 <= days <= 380):
                    # Not a full fiscal year -- annual mode has no use
                    # for a shorter window, and (unlike quarterly mode)
                    # there's no cumulative-chain derivation to feed.
                    continue
            else:
                chain = by_start.setdefault(start, {})
                existing_chain_entry = chain.get(end)
                if existing_chain_entry is None or e["filed"] > existing_chain_entry["filed"]:
                    chain[end] = e

                if not (75 <= days <= 100):
                    # Not a standalone quarter -- it may still be useful
                    # as a cumulative point in the second pass below,
                    # but it doesn't go directly into by_end.
                    continue

        end = e["end"]
        existing = by_end.get(end)
        if existing is None or e["filed"] > existing["filed"]:
            by_end[end] = e

    if kind == "duration" and not annual:
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


def build_statement(
    cik: str, statement: str, n_periods: int = 4, period_type: str = "quarterly"
) -> pd.DataFrame:
    """Build a statement as a DataFrame: rows are line items (raw fields
    from STATEMENTS, followed by that statement's DERIVED_METRICS),
    columns are the most recent `n_periods` fiscal period-end dates,
    most recent first.

    `period_type` (Tier 4): "quarterly" (the default) builds from 10-Q
    filings (plus 10-K quarters), the same as always. "annual" builds
    from 10-K filings only -- each column is a fiscal YEAR, not a
    quarter, e.g. for a company with 6 years of history and
    n_periods=4, you get its 4 most recent fiscal years. See
    _quarterly_points() for how the two differ under the hood."""
    if statement not in STATEMENTS:
        raise SecEdgarError(f"Unknown statement '{statement}'. Choose one of {list(STATEMENTS)}.")
    if period_type not in ("quarterly", "annual"):
        raise SecEdgarError(f"Unknown period_type '{period_type}'. Choose 'quarterly' or 'annual'.")

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
            tag_points = _quarterly_points(entries, item.kind, period_type=period_type)
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


# ---------------------------------------------------------------------------
# Comparables Analysis (Tier 3) -- Price / Market Cap / EV / Sales /
# EBITDA / EBIT / Earnings / Beta / valuation multiples across up to 10
# companies at once, matching the standard sell-side comps table
# format. See the module docstring for why market data comes from
# Finviz rather than SEC, and Charter Section 5 for the full design
# writeup (period alignment, TTM math, the star-marking rule).
#
# Note on naming: "TEV" (Total Enterprise Value) and "EV" (Enterprise
# Value) mean the same thing -- Kevin asked for the shorter "EV" label
# to match how he labels it on his own comps sheets, so that's what
# this column (and the EV/Sales, EV/EBITDA, EV/EBIT multiples that
# divide by it) is called everywhere below.
# ---------------------------------------------------------------------------

COMPARABLES_COLUMNS = [
    "Price",
    "Market Cap",
    "EV",
    "Sales",
    "EBITDA",
    "EBIT",
    "Earnings",
    "Beta",
    "EV/Sales",
    "EV/EBITDA",
    "EV/EBIT",
    "P/E",
]

# Average/Median only ever get computed over these four -- a company's
# raw dollar figures (Sales, Market Cap, ...) aren't meaningfully
# "averaged" across a comp set the way a valuation multiple is; that's
# exactly what the real comps table screenshot this was built from
# shows too (blank Market Data / Financial Data cells on those rows).
_COMPARABLES_MULTIPLE_COLUMNS = ["EV/Sales", "EV/EBITDA", "EV/EBIT", "P/E"]

# Beta gets an Average too (a plain arithmetic mean, same as the four
# multiples above), but no Median -- Kevin only asked for the average,
# and it isn't part of the original screenshot's Valuation block, so it
# gets its own small list rather than joining _COMPARABLES_MULTIPLE_COLUMNS
# (which drives BOTH Average and Median).
_COMPARABLES_AVERAGE_ONLY_COLUMNS = ["Beta"]


def get_market_data(ticker: str) -> dict[str, float | None]:
    """Live market data for one ticker, from Finviz: current share
    price, market capitalization, beta, and Finviz's own P/E, P/B, P/S.
    finvizfinance's raw=False option converts Finviz's formatted page
    text ("168.04B", "4.02%") into plain floats for us, so every value
    here is already a number (or None if Finviz didn't have it).

    Finviz isn't a documented API -- finvizfinance works by reading
    finviz.com's own quote page, so ANY failure here (bad ticker,
    finviz.com being briefly unreachable, finviz changing their page
    layout) gets caught broadly and turned into one clear
    MarketDataError, rather than leaking a scraping-library-specific
    exception type up into app.py."""
    try:
        quote = _FinvizQuote(ticker)
        fundament = quote.ticker_fundament(raw=False)
    except Exception as e:  # noqa: BLE001 -- deliberately broad, see docstring above
        raise MarketDataError(f"Couldn't get Finviz market data for '{ticker}': {e}") from e

    return {
        "Price": fundament.get("Price"),
        "Market Cap": fundament.get("Market Cap"),
        "Beta": fundament.get("Beta"),
        "P/E": fundament.get("P/E"),
        "P/B": fundament.get("P/B"),
        "P/S": fundament.get("P/S"),
    }


def _company_period_is_annual_only(cik: str) -> bool:
    """True when this company's most recently available quarter is
    really a full fiscal year total -- either because their latest
    filing is a 10-K with no 10-Q having followed it yet, or because
    that period only exists as a DERIVED value (see _quarterly_points'
    year-to-date subtraction) rather than a directly reported discrete
    quarter. Either way, a "quarter" built from it actually spans more
    than three months. build_comparables_table() star-marks a
    company's name when this is true (Charter Section 5)."""
    company_facts = get_company_facts(cik)
    revenue_item = next(item for item in STATEMENTS["Income Statement"] if item.label == "Total revenue")

    points: dict[str, dict] = {}
    for tag in revenue_item.tags:
        entries = _facts_for_tag(company_facts, tag, unit=revenue_item.unit)
        for end, point in _quarterly_points(entries, revenue_item.kind).items():
            points.setdefault(end, point)

    if not points:
        return False

    latest_end = max(points, key=lambda end: pd.Timestamp(end))
    latest_point = points[latest_end]
    return bool(latest_point.get("derived")) or latest_point.get("form") == "10-K"


def _ttm_sum(df: pd.DataFrame, label: str) -> float | None:
    """Sum of a duration-type row's available quarters in `df`, which
    is expected to already be a build_statement(..., n_periods=4)
    result -- so this sums up to the trailing four reported quarters.
    A company with fewer than four quarters of data (a recent IPO, or
    a gap our tag coverage hasn't caught yet -- see
    TAG_COVERAGE_GUIDE.md) sums whatever it has rather than padding the
    rest with a guess, which understates a true trailing-twelve-months
    figure; that's a known, honest limitation, not a bug."""
    if label not in df.index:
        return None
    values = df.loc[label].dropna()
    if values.empty:
        return None
    return float(values.sum())


def comparables_field_formats() -> dict[str, str]:
    """Maps each build_comparables_table() column to a display-format
    hint: 'price' ($/share, 2 decimals), 'dollar_m' (a dollar figure
    shown in millions -- the standard way a comps table is presented;
    nobody prints a market cap out to the individual dollar), 'beta'
    (a plain two-decimal number, e.g. "0.98" -- betas are already a
    small unitless ratio, so unlike 'multiple' there's no 'x' suffix),
    or 'multiple' (one decimal place plus an 'x' suffix, e.g. "14.1x")."""
    formats = {"Price": "price", "Beta": "beta"}
    for col in ["Market Cap", "EV", "Sales", "EBITDA", "EBIT", "Earnings"]:
        formats[col] = "dollar_m"
    for col in _COMPARABLES_MULTIPLE_COLUMNS:
        formats[col] = "multiple"
    return formats


def build_comparables_table(tickers: list[str]) -> tuple[pd.DataFrame, dict[str, str]]:
    """The Comparables Analysis table: Price / Market Cap / EV / Sales /
    EBITDA / EBIT / Earnings / Beta / EV-Sales / EV-EBITDA / EV-EBIT /
    P-E for up to MAX_COMPARABLES_TICKERS tickers, plus Average and
    Median summary rows over the four valuation-multiple columns (and
    an Average-only row for Beta) -- built to match a standard
    sell-side comps table exactly.

    Definitions (Charter Section 5 has the full writeup):
    - Sales / EBITDA / EBIT / Earnings are trailing-twelve-months (TTM)
      -- the sum of each company's own most recently reported four
      quarters, NOT a calendar-aligned year, so two companies with
      different fiscal year ends still compare fairly. EBITDA =
      Operating income + Depreciation and amortization; EBIT is
      treated as Operating income directly, the standard shorthand
      every comps table uses.
    - Price, Market Cap, and Beta come from Finviz (see the module
      docstring). EV (Enterprise Value -- some call it TEV, Total
      Enterprise Value, same thing) = Market Cap + Total debt - Cash
      and cash equivalents, with a missing debt or cash figure treated
      as $0 rather than leaving EV blank -- a company with no reported
      debt tag usually means it has none, not that the number is
      unknown.
    - P/E here is Market Cap / Earnings (TTM), computed from OUR OWN
      EDGAR-sourced earnings figure -- not Finviz's own P/E -- so it's
      built the same consistent way as the other three multiples. This
      can differ slightly from what Finviz's own quote page shows,
      which may use adjusted or forward EPS.
    - A company name gets a trailing " *" when its most recent period
      is really a full fiscal year (see _company_period_is_annual_only)
      -- its TTM figures still sum four real quarters, but the
      balance-sheet snapshot (Total debt, Cash) is as of that fiscal
      year-end, possibly a quarter or two stale.

    Returns (dataframe, errors) -- errors maps any ticker that failed
    (bad symbol, no SEC data, a Finviz lookup failure, ...) to a plain
    -English reason, with that ticker simply left out of the table
    rather than failing the whole request. One bad ticker in a list of
    ten should never cost you the other nine.
    """
    tickers = [t.strip().upper() for t in tickers if t.strip()]
    if len(tickers) > MAX_COMPARABLES_TICKERS:
        raise SecEdgarError(
            f"Comparables Analysis supports at most {MAX_COMPARABLES_TICKERS} tickers at a time "
            f"(got {len(tickers)})."
        )

    rows: dict[str, dict[str, float | None]] = {}
    errors: dict[str, str] = {}

    for ticker in tickers:
        try:
            cik = get_cik_for_ticker(ticker)
            name = company_name(cik) or ticker
            if _company_period_is_annual_only(cik):
                name = f"{name} *"

            income_df = build_statement(cik, "Income Statement", n_periods=4)
            cash_flow_df = build_statement(cik, "Cash Flow Statement", n_periods=4)
            balance_df = build_statement(cik, "Balance Sheet", n_periods=1)

            sales = _ttm_sum(income_df, "Total revenue")
            ebit = _ttm_sum(income_df, "Operating income")
            earnings = _ttm_sum(income_df, "Net income")
            d_and_a = _ttm_sum(cash_flow_df, "Depreciation and amortization")
            ebitda = ebit + d_and_a if ebit is not None and d_and_a is not None else None

            latest_bs_col = balance_df.columns[0] if len(balance_df.columns) else None
            total_debt = balance_df.loc["Total debt", latest_bs_col] if latest_bs_col is not None else None
            cash = (
                balance_df.loc["Cash and cash equivalents", latest_bs_col]
                if latest_bs_col is not None
                else None
            )
            if pd.isnull(total_debt):
                total_debt = None
            if pd.isnull(cash):
                cash = None

            market = get_market_data(ticker)
            price = market.get("Price")
            market_cap = market.get("Market Cap")
            beta = market.get("Beta")

            ev = market_cap + (total_debt or 0.0) - (cash or 0.0) if market_cap is not None else None

            rows[name] = {
                "Price": price,
                "Market Cap": market_cap,
                "EV": ev,
                "Sales": sales,
                "EBITDA": ebitda,
                "EBIT": ebit,
                "Earnings": earnings,
                "Beta": beta,
                "EV/Sales": _safe_div(ev, sales),
                "EV/EBITDA": _safe_div(ev, ebitda),
                "EV/EBIT": _safe_div(ev, ebit),
                "P/E": _safe_div(market_cap, earnings),
            }
        except (SecEdgarError, MarketDataError) as e:
            errors[ticker] = str(e)
        except Exception as e:  # noqa: BLE001 -- last-resort isolation, see docstring above
            errors[ticker] = f"Unexpected error: {e}"

    df = pd.DataFrame.from_dict(rows, orient="index", columns=COMPARABLES_COLUMNS)

    if not df.empty:
        summary: dict[str, dict[str, float | None]] = {"Average": {}, "Median": {}}
        for col in _COMPARABLES_MULTIPLE_COLUMNS:
            values = df[col].dropna()
            summary["Average"][col] = float(values.mean()) if not values.empty else None
            summary["Median"][col] = float(values.median()) if not values.empty else None
        for col in _COMPARABLES_AVERAGE_ONLY_COLUMNS:  # Beta: Average only, no Median
            values = df[col].dropna()
            summary["Average"][col] = float(values.mean()) if not values.empty else None
        for label in ("Average", "Median"):
            df.loc[label] = {col: summary[label].get(col) for col in COMPARABLES_COLUMNS}

    return df, errors


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

    # Comparables Analysis smoke test -- pass a few tickers to try it,
    # e.g.: python sec_edgar.py META GOOG AMZN MSFT ORCL NVDA
    if len(sys.argv) > 2:
        print("\n=== Comparables Analysis ===")
        comps_df, comps_errors = build_comparables_table(sys.argv[1:])
        print(comps_df)
        if comps_errors:
            print("\nSkipped tickers:")
            for bad_ticker, reason in comps_errors.items():
                print(f"  {bad_ticker}: {reason}")
