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
            # Revenues is deliberately absent to test tag fallback.
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {
                    "USD": [
                        _duration("2026-01-01", "2026-03-31", 500),  # ~90 day quarter: keep
                        _duration("2026-04-01", "2026-06-30", 550),  # ~91 day quarter: keep
                        _duration("2026-01-01", "2026-06-30", 1050),  # 6-month cumulative: drop
                    ]
                }
            },
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
    # "Total revenue" should have fallen back from "Revenues" (missing)
    # to "RevenueFromContractWithCustomerExcludingAssessedTax".
    assert df2.loc["Total revenue", "2026-06-30"] == 550


if __name__ == "__main__":
    test_instant_prefers_latest_filed_value()
    test_duration_filters_out_non_quarterly_windows()

    class _FakeMonkeypatch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    test_build_statement_falls_back_between_tags(_FakeMonkeypatch())
    print("All tests passed.")
