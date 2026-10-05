"""
Tests for the Annual (10-K) vs. Quarterly (10-Q) period_type support
(Tier 4): the period_type="annual" branch in _quarterly_points() and
build_statement(). Like test_sec_edgar.py, these build small fake
fact entries by hand and never touch the network.

Run with:  python -m pytest tests/test_period_type.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def test_quarterly_points_annual_mode_keeps_only_full_year_10k_entries():
    entries = [
        _duration("2026-01-01", "2026-03-31", 100, form="10-Q"),  # ~90 days -- a quarter
        _duration("2025-01-01", "2025-12-31", 900, form="10-K"),  # ~365 days -- a fiscal year
        _duration("2024-01-01", "2024-12-31", 800, form="10-K"),  # another fiscal year
    ]
    annual_points = sec_edgar._quarterly_points(entries, "duration", period_type="annual")
    assert set(annual_points.keys()) == {"2025-12-31", "2024-12-31"}
    assert annual_points["2025-12-31"]["val"] == 900

    quarterly_points = sec_edgar._quarterly_points(entries, "duration")  # default period_type
    assert set(quarterly_points.keys()) == {"2026-03-31"}


def test_quarterly_points_annual_mode_requires_form_10k_even_at_the_right_duration():
    # A ~365-day entry filed under a 10-Q (unusual, but XBRL allows any
    # form to carry any duration) should NOT count as an annual period
    # -- annual mode specifically wants 10-K-reported fiscal years, per
    # Charter Section 5 / the Tier 4 checklist ("Annual (10-K) vs.
    # quarterly (10-Q) toggle").
    entries = [_duration("2025-01-01", "2025-12-31", 900, form="10-Q")]
    assert sec_edgar._quarterly_points(entries, "duration", period_type="annual") == {}


def test_quarterly_points_annual_mode_never_derives_from_cumulative_chains():
    # The Q4-from-9-month-and-full-year derivation (see
    # test_sec_edgar.py) is a quarterly-only concept -- in annual mode
    # the full-year entry is already exactly what's wanted, directly
    # reported, so no point should ever come back marked "derived".
    entries = [
        _duration("2026-01-01", "2026-09-30", 700, form="10-Q"),  # 9-month cumulative
        _duration("2026-01-01", "2026-12-31", 950, form="10-K"),  # full year
    ]
    annual_points = sec_edgar._quarterly_points(entries, "duration", period_type="annual")
    assert annual_points["2026-12-31"]["val"] == 950
    assert not annual_points["2026-12-31"].get("derived")


def test_quarterly_points_instant_annual_mode_only_keeps_10k_snapshots():
    # Balance Sheet fields are all "instant" -- in annual mode these
    # should only pick up fiscal-year-end snapshots (10-K), not every
    # quarter-end balance a 10-Q also reports. Without this, Annual
    # mode wouldn't actually change the Balance Sheet at all.
    entries = [
        _instant("2026-03-31", 100, form="10-Q"),
        _instant("2025-12-31", 90, form="10-K"),
    ]
    annual_points = sec_edgar._quarterly_points(entries, "instant", period_type="annual")
    assert set(annual_points.keys()) == {"2025-12-31"}

    quarterly_points = sec_edgar._quarterly_points(entries, "instant")  # default: both allowed
    assert set(quarterly_points.keys()) == {"2026-03-31", "2025-12-31"}


FAKE_ANNUAL_FACTS = {
    "entityName": "Fake Annual Corp",
    "facts": {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {
                    "USD": [
                        _duration("2026-01-01", "2026-03-31", 100, form="10-Q"),  # a quarter
                        _duration("2025-01-01", "2025-12-31", 900, form="10-K"),  # FY2025
                        _duration("2024-01-01", "2024-12-31", 800, form="10-K"),  # FY2024
                        _duration("2023-01-01", "2023-12-31", 700, form="10-K"),  # FY2023
                    ]
                }
            },
            "Assets": {
                "units": {
                    "USD": [
                        _instant("2026-03-31", 5000, form="10-Q"),
                        _instant("2025-12-31", 4800, form="10-K"),
                        _instant("2024-12-31", 4600, form="10-K"),
                    ]
                }
            },
        }
    },
}


def test_build_statement_annual_mode_returns_fiscal_year_columns(monkeypatch):
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_ANNUAL_FACTS)

    annual_df = sec_edgar.build_statement("0000000000", "Income Statement", n_periods=2, period_type="annual")
    assert list(annual_df.columns) == ["2025-12-31", "2024-12-31"]
    assert annual_df.loc["Total revenue", "2025-12-31"] == 900
    assert annual_df.loc["Total revenue", "2024-12-31"] == 800

    # Quarterly mode (the default) on the same fake data should be
    # unaffected -- still just the one quarter, ignoring the 10-K
    # fiscal-year entries entirely.
    quarterly_df = sec_edgar.build_statement("0000000000", "Income Statement", n_periods=2)
    assert list(quarterly_df.columns) == ["2026-03-31"]


def test_build_statement_annual_mode_also_restricts_instant_balance_sheet_fields(monkeypatch):
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_ANNUAL_FACTS)

    annual_df = sec_edgar.build_statement("0000000000", "Balance Sheet", n_periods=2, period_type="annual")
    assert list(annual_df.columns) == ["2025-12-31", "2024-12-31"]
    assert annual_df.loc["Total assets", "2025-12-31"] == 4800


def test_build_statement_rejects_an_unknown_period_type(monkeypatch):
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_ANNUAL_FACTS)
    try:
        sec_edgar.build_statement("0000000000", "Income Statement", period_type="yearly")
        assert False, "expected a SecEdgarError for an unknown period_type"
    except sec_edgar.SecEdgarError as e:
        assert "period_type" in str(e)
