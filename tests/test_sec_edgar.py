"""
Tests for sec_edgar.py that don't touch the network at all.

Instead of downloading real data from SEC, we build a small fake
"company facts" blob by hand (same shape SEC's API returns) and hand it
to build_statement() directly. This lets us check the tricky logic --
picking quarterly-only entries, falling back between candidate tags --
without depending on the internet or on SEC's data changing over time.

Run with:  python -m pytest tests/test_sec_edgar.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
import sec_edgar  # noqa: E402


def _instant(end, val, form="10-Q", filed=None):
    return {"end": end, "val": val, "form": form, "filed": filed or end, "accn": "x", "fy": 2026, "fp": "Q2"}


def _duration(start, end, val, form="10-Q", filed=None):
    return {
        "start": start,
        "end": end,
        "val": val,
        "form": form,
        "filed": filed or end,
        "accn": "x",
        "fy": 2026,
        "fp": "Q2",
    }


FAKE_FACTS = {
    "entityName": "Fake Corp",
    "facts": {
        "us-gaap": {
            "Assets": {
                "units": {
                    "USD": [
                        _instant("2026-03-31", 100),
                        _instant("2026-06-30", 110),
                        # A duplicate/restated value for the same period,
                        # filed later -- build_statement should prefer
                        # this one because it has the latest "filed" date.
                        _instant("2026-06-30", 111, filed="2026-08-01"),
                    ]
                }
            },
            # "Revenues" has data, but only for an OLD quarter -- this is
            # the shape of the real Meta bug: a company retires a tag
            # and switches to a new one, so the old tag still has SOME
            # data, just nothing recent. build_statement should merge in
            # the newer tag's more recent periods rather than stopping
            # at "Revenues" just because it found something.
            "Revenues": {
                "units": {"USD": [_duration("2019-01-01", "2019-03-31", 999, filed="2019-04-26")]}
            },
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {
                    "USD": [
                        _duration("2026-01-01", "2026-03-31", 500),  # ~90 day quarter: keep
                        _duration("2026-04-01", "2026-06-30", 550),  # ~91 day quarter: keep
                        _duration("2026-01-01", "2026-06-30", 1050),  # 6-month cumulative: drop
                    ]
                }
            },
            # EPS is reported under unit "USD/shares", not "USD" -- this
            # checks build_statement actually looks in the right unit
            # bucket instead of finding nothing and going blank.
            "EarningsPerShareBasic": {
                "units": {"USD/shares": [_duration("2026-04-01", "2026-06-30", 6.23)]}
            },
            "WeightedAverageNumberOfSharesOutstandingBasic": {
                "units": {"shares": [_duration("2026-04-01", "2026-06-30", 2543)]}
            },
            # The rest of these exist only to give the derived-metrics
            # tests (gross/operating margin, free cash flow, total debt)
            # something real to compute from for 2026-06-30.
            "GrossProfit": {"units": {"USD": [_duration("2026-04-01", "2026-06-30", 220)]}},
            # Deliberately a DIFFERENT number than 550 - 220 would give,
            # so the fallback-doesn't-override test can prove the
            # reported "GrossProfit" value (220) wins even though
            # Revenue - Cost of revenue would compute something else.
            "CostOfRevenue": {"units": {"USD": [_duration("2026-04-01", "2026-06-30", 300)]}},
            "OperatingIncomeLoss": {"units": {"USD": [_duration("2026-04-01", "2026-06-30", 110)]}},
            "NetIncomeLoss": {"units": {"USD": [_duration("2026-04-01", "2026-06-30", 88)]}},
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {"USD": [_duration("2026-04-01", "2026-06-30", 130)]}
            },
            "PaymentsToAcquirePropertyPlantAndEquipment": {
                "units": {"USD": [_duration("2026-04-01", "2026-06-30", 40)]}
            },
            "LongTermDebtNoncurrent": {"units": {"USD": [_instant("2026-06-30", 300)]}},
            "LongTermDebtCurrent": {"units": {"USD": [_instant("2026-06-30", 50)]}},
            "StockholdersEquity": {"units": {"USD": [_instant("2026-06-30", 700)]}},
            "LiabilitiesCurrent": {"units": {"USD": [_instant("2026-06-30", 90)]}},
            "Liabilities": {"units": {"USD": [_instant("2026-06-30", 440)]}},
            "AssetsCurrent": {"units": {"USD": [_instant("2026-06-30", 180)]}},
        }
    },
}


def test_instant_prefers_latest_filed_value():
    points = sec_edgar._quarterly_points(FAKE_FACTS["facts"]["us-gaap"]["Assets"]["units"]["USD"], "instant")
    assert points["2026-06-30"]["val"] == 111


def test_duration_filters_out_non_quarterly_windows():
    entries = FAKE_FACTS["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
    points = sec_edgar._quarterly_points(entries, "duration")
    assert set(points.keys()) == {"2026-03-31", "2026-06-30"}
    assert points["2026-06-30"]["val"] == 550


def test_build_statement_falls_back_between_tags(monkeypatch):
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_FACTS)

    df = sec_edgar.build_statement("0000000000", "Balance Sheet", n_periods=2)
    assert list(df.columns) == ["2026-06-30", "2026-03-31"]
    assert df.loc["Total assets", "2026-06-30"] == 111
    assert df.loc["Total assets", "2026-03-31"] == 100

    df2 = sec_edgar.build_statement("0000000000", "Income Statement", n_periods=2)
    # "Total revenue" should have fallen back from "Revenues" -- which
    # DOES have data, just only for an old 2019 quarter that isn't one
    # of the periods being built -- to
    # "RevenueFromContractWithCustomerExcludingAssessedTax" for the
    # current periods. This is the merge behavior, not "first tag with
    # any data wins" (which would leave 2026-06-30 blank).
    assert df2.loc["Total revenue", "2026-06-30"] == 550
    assert df2.loc["Total revenue", "2026-03-31"] == 500


def test_build_statement_reads_eps_and_share_counts_from_their_own_units(monkeypatch):
    # EPS is reported under unit "USD/shares" and share counts under
    # "shares", not "USD" like every other line item. Before this was
    # fixed, _facts_for_tag hardcoded "USD" and both rows came back
    # entirely blank.
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_FACTS)
    df = sec_edgar.build_statement("0000000000", "Income Statement", n_periods=1)
    assert df.loc["EPS, basic", "2026-06-30"] == 6.23
    assert df.loc["Weighted avg. shares, basic", "2026-06-30"] == 2543


def test_gross_profit_fallback_when_not_reported(monkeypatch):
    # Some companies never report a "GrossProfit" tag at all -- their
    # 10-Q just goes straight from Revenue to expense lines with no
    # subtotal. In that case Gross profit should be computed as
    # Total revenue - Cost of revenue, not come back blank.
    facts = {
        "entityName": "No Gross Profit Tag Corp",
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [_duration("2026-04-01", "2026-06-30", 1000)]}
                },
                "CostOfRevenue": {"units": {"USD": [_duration("2026-04-01", "2026-06-30", 400)]}},
                # No "GrossProfit" tag.
            }
        },
    }
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: facts)
    df = sec_edgar.build_statement("0000000000", "Income Statement", n_periods=1)
    assert df.loc["Gross profit", "2026-06-30"] == 1000 - 400
    # "Gross margin %" reads Gross profit -- it should pick up the
    # fallback value too, since by the time derived metrics run, Gross
    # profit is already filled in either way.
    assert df.loc["Gross margin %", "2026-06-30"] == (1000 - 400) / 1000


def test_gross_profit_fallback_never_overrides_a_reported_value(monkeypatch):
    # FAKE_FACTS reports GrossProfit=220 directly, AND has a
    # CostOfRevenue=300 that would compute a DIFFERENT number
    # (550 - 300 = 250) via the fallback. The actually-reported 220 must
    # win -- the fallback only fills a period that came back with
    # nothing at all.
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_FACTS)
    df = sec_edgar.build_statement("0000000000", "Income Statement", n_periods=1)
    assert df.loc["Gross profit", "2026-06-30"] == 220


# ---------------------------------------------------------------------------
# Derived metrics (Tier 1)
# ---------------------------------------------------------------------------

def test_safe_helpers_handle_missing_values():
    assert sec_edgar._safe_div(10, 2) == 5
    assert sec_edgar._safe_div(10, None) is None
    assert sec_edgar._safe_div(10, 0) is None  # would otherwise raise ZeroDivisionError
    assert sec_edgar._safe_add(1, None, 2) == 3
    assert sec_edgar._safe_add(None, None) is None
    assert sec_edgar._safe_sub(5, None) is None


def test_income_statement_margins(monkeypatch):
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_FACTS)
    df = sec_edgar.build_statement("0000000000", "Income Statement", n_periods=2)

    # 2026-06-30 has Gross profit/Operating income/Net income in the
    # fixture, so every margin should compute cleanly.
    assert df.loc["Gross margin %", "2026-06-30"] == 220 / 550
    assert df.loc["Operating margin %", "2026-06-30"] == 110 / 550
    assert df.loc["Net margin %", "2026-06-30"] == 88 / 550

    # 2026-03-31 has a "Total revenue" but none of the other inputs a
    # margin needs -- the formula should return None (via _safe_div),
    # not raise or silently print 0.
    assert pd.isna(df.loc["Gross margin %", "2026-03-31"])


def test_cash_flow_free_cash_flow(monkeypatch):
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_FACTS)
    df = sec_edgar.build_statement("0000000000", "Cash Flow Statement", n_periods=1)
    assert df.loc["Free cash flow", "2026-06-30"] == 130 - 40


def test_balance_sheet_total_debt_and_ratios(monkeypatch):
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_FACTS)
    df = sec_edgar.build_statement("0000000000", "Balance Sheet", n_periods=1)

    assert df.loc["Total debt", "2026-06-30"] == 300 + 50
    assert df.loc["LT (non-current) liabilities", "2026-06-30"] == 440 - 90
    # Debt / equity depends on "Total debt" (an earlier derived metric
    # in the same list) -- this checks that dependency actually resolves.
    assert df.loc["Debt / equity", "2026-06-30"] == (300 + 50) / 700
    assert df.loc["Current ratio", "2026-06-30"] == 180 / 90


def test_field_formats_cover_every_row(monkeypatch):
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_FACTS)
    for statement in sec_edgar.STATEMENTS:
        df = sec_edgar.build_statement("0000000000", statement, n_periods=1)
        formats = sec_edgar.field_formats(statement)
        # Every row build_statement() can produce must have a format
        # hint, or app.py's display formatting would KeyError on it.
        assert set(df.index) <= set(formats.keys())
    assert sec_edgar.field_formats("Income Statement")["Gross margin %"] == "pct"
    assert sec_edgar.field_formats("Balance Sheet")["Debt / equity"] == "ratio"
    assert sec_edgar.field_formats("Balance Sheet")["Total assets"] == "dollar"


if __name__ == "__main__":
    test_instant_prefers_latest_filed_value()
    test_duration_filters_out_non_quarterly_windows()
    test_safe_helpers_handle_missing_values()

    class _FakeMonkeypatch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    test_build_statement_falls_back_between_tags(_FakeMonkeypatch())
    test_build_statement_reads_eps_and_share_counts_from_their_own_units(_FakeMonkeypatch())
    test_gross_profit_fallback_when_not_reported(_FakeMonkeypatch())
    test_gross_profit_fallback_never_overrides_a_reported_value(_FakeMonkeypatch())
    test_income_statement_margins(_FakeMonkeypatch())
    test_cash_flow_free_cash_flow(_FakeMonkeypatch())
    test_balance_sheet_total_debt_and_ratios(_FakeMonkeypatch())
    test_field_formats_cover_every_row(_FakeMonkeypatch())
    print("All tests passed.")
