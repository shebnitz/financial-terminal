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
            # Working-capital-change tags for 2026-06-30. The three
            # ASSET-side ones (receivable, prepaid, other assets) are
            # filed as POSITIVE = "the asset balance grew" -- the
            # opposite sign from how the cash flow statement displays
            # them (an asset growing is a USE of cash, shown negative).
            # This is the exact shape verified against Meta's own real
            # H1-2026 filing: "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets"
            # reports +3,230M there even though the statement prints
            # (3,230). The three LIABILITY-side ones (payable, accrued,
            # other liabilities) need no flip -- their raw sign already
            # matches what's printed.
            "IncreaseDecreaseInAccountsReceivable": {
                "units": {"USD": [_duration("2026-04-01", "2026-06-30", 400)]}
            },
            "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets": {
                "units": {"USD": [_duration("2026-04-01", "2026-06-30", 100)]}
            },
            "IncreaseDecreaseInOtherOperatingAssets": {
                "units": {"USD": [_duration("2026-04-01", "2026-06-30", 50)]}
            },
            "IncreaseDecreaseInAccountsPayableTrade": {
                "units": {"USD": [_duration("2026-04-01", "2026-06-30", -80)]}
            },
            "IncreaseDecreaseInAccruedLiabilities": {
                "units": {"USD": [_duration("2026-04-01", "2026-06-30", 200)]}
            },
            "IncreaseDecreaseInOtherOperatingLiabilities": {
                "units": {"USD": [_duration("2026-04-01", "2026-06-30", -30)]}
            },
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


def test_working_capital_line_items_flip_sign_for_asset_side_tags_only(monkeypatch):
    # The three ASSET-side tags are filed as +400/+100/+50 (the asset
    # balance grew) but should DISPLAY as negative (a growing asset
    # uses cash) -- LineItem.sign=-1 does that flip. The three
    # LIABILITY-side tags are filed already matching what's displayed,
    # so they should come through unchanged.
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_FACTS)
    df = sec_edgar.build_statement("0000000000", "Cash Flow Statement", n_periods=1)

    assert df.loc["Change in accounts receivable", "2026-06-30"] == -400
    assert df.loc["Change in prepaid expenses & other current assets", "2026-06-30"] == -100
    assert df.loc["Change in other assets", "2026-06-30"] == -50
    assert df.loc["Change in accounts payable", "2026-06-30"] == -80  # unchanged, already matches
    assert df.loc["Change in accrued expenses & other current liabilities", "2026-06-30"] == 200
    assert df.loc["Change in other liabilities", "2026-06-30"] == -30


def test_change_in_working_capital_sums_all_six_components(monkeypatch):
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: FAKE_FACTS)
    df = sec_edgar.build_statement("0000000000", "Cash Flow Statement", n_periods=1)
    # Sum of the SIGN-CORRECTED values above, not the raw filed ones --
    # -400 + -100 + -50 + -80 + 200 + -30 = -460.
    assert df.loc["Change in working capital", "2026-06-30"] == -460


def test_property_and_equipment_falls_back_to_the_finance_lease_combined_tag(monkeypatch):
    # Meta (and several other large companies) stopped using the plain
    # "PropertyPlantAndEquipmentNet" tag years ago in favor of one that
    # also folds in finance lease right-of-use assets. Only the second,
    # longer tag has data here -- build_statement should still find it
    # via the same tag-merge logic used for Revenue.
    facts = {
        "entityName": "No Plain PPE Tag Corp",
        "facts": {
            "us-gaap": {
                "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization": {
                    "units": {"USD": [_instant("2026-06-30", 225724)]}
                },
            }
        },
    }
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: facts)
    df = sec_edgar.build_statement("0000000000", "Balance Sheet", n_periods=1)
    assert df.loc["Property and equipment, net", "2026-06-30"] == 225724


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


# ---------------------------------------------------------------------------
# Field toggle & preset system (Tier 2)
# ---------------------------------------------------------------------------

def test_statement_categories_cover_every_field_exactly_once():
    # statement_categories() groups fields for the sidebar -- if a
    # field went missing (or got double-counted) in the grouping, a
    # checkbox for it would silently vanish from the app.
    for statement in sec_edgar.STATEMENTS:
        labels = sec_edgar.statement_field_labels(statement)
        grouped_labels = [label for group in sec_edgar.statement_categories(statement).values() for label in group]
        assert sorted(grouped_labels) == sorted(labels)
        assert len(grouped_labels) == len(set(grouped_labels))  # no field counted twice


def test_every_preset_field_is_a_real_label_somewhere():
    # Every preset (Concise, Full, Free Cash Flow Mode, Profitability
    # Mode, Leverage Mode) is hand-written as a set of label strings --
    # this catches a typo'd label before it ships (it would otherwise
    # just silently never match any field, and that field would never
    # get checked by the preset).
    all_labels = sec_edgar._all_field_labels()
    for preset in sec_edgar.PRESETS:
        assert preset.fields <= all_labels, f"{preset.label} has unknown label(s): {preset.fields - all_labels}"


def test_full_preset_is_every_field():
    all_labels = sec_edgar._all_field_labels()
    assert sec_edgar.PRESETS_BY_LABEL["Full"].fields == all_labels


def test_preset_default_fields_only_returns_fields_that_statement_has():
    # "Concise" includes fields from all three statements (Total
    # revenue is Income Statement, Total assets is Balance Sheet, Free
    # cash flow is Cash Flow Statement, ...). Asking for Concise's
    # defaults scoped to just the Balance Sheet should return only the
    # Concise fields that ARE Balance Sheet fields -- not "Total
    # revenue", which the Balance Sheet doesn't have.
    bs_defaults = sec_edgar.preset_default_fields("Concise", "Balance Sheet")
    assert "Total assets" in bs_defaults
    assert "Total debt" in bs_defaults
    assert "Total revenue" not in bs_defaults  # an Income Statement field
    assert bs_defaults <= set(sec_edgar.statement_field_labels("Balance Sheet"))


def test_preset_default_fields_unknown_preset_defaults_to_everything_on():
    # A typo'd or stale preset name (e.g. from an old session_state
    # value after a preset gets renamed) should show everything rather
    # than silently hiding the whole statement.
    result = sec_edgar.preset_default_fields("Not A Real Preset", "Income Statement")
    assert result == set(sec_edgar.statement_field_labels("Income Statement"))


# ---------------------------------------------------------------------------
# Deriving a standalone quarter from year-to-date cumulative entries --
# this is the fix for fields META only ever tags cumulatively (verified
# against Meta's own real filed data for "Proceeds from debt issuance"
# and "Purchases of investments").
# ---------------------------------------------------------------------------

def test_quarterly_points_derives_q4_from_9_month_and_full_year_cumulative_totals():
    # Meta's actual real-world shape for "proceeds from issuance of
    # long-term debt" in FY2025: nine months cumulative = $0 (no debt
    # issued in the first three quarters), full year = $29,906M -- so
    # all of it happened in Q4, derivable as FY minus 9-month.
    entries = [
        _duration("2025-01-01", "2025-09-30", 0, form="10-Q"),
        _duration("2025-01-01", "2025-12-31", 29_906_000_000, form="10-K"),
    ]
    points = sec_edgar._quarterly_points(entries, "duration")
    assert points["2025-12-31"]["val"] == 29_906_000_000
    assert points["2025-12-31"]["derived"] is True
    # The 9-month point itself never gets a standalone value -- there's
    # no earlier cumulative point in the chain to subtract it from.
    assert "2025-09-30" not in points


def test_quarterly_points_derives_q2_from_discrete_q1_and_6_month_cumulative():
    # Meta's real shape for "Purchases of investments" in FY2026: a
    # genuine standalone Q1 (~90 days) plus a 6-month cumulative total
    # for H1 -- Q2 alone is H1 minus Q1.
    entries = [
        _duration("2026-01-01", "2026-03-31", 32_978_000_000, form="10-Q"),  # discrete Q1
        _duration("2026-01-01", "2026-06-30", 75_592_000_000, form="10-Q"),  # H1 cumulative
    ]
    points = sec_edgar._quarterly_points(entries, "duration")
    assert points["2026-03-31"]["val"] == 32_978_000_000
    assert "derived" not in points["2026-03-31"]  # a real reported quarter, not derived
    assert points["2026-06-30"]["val"] == 42_614_000_000  # 75,592M - 32,978M
    assert points["2026-06-30"]["derived"] is True


def test_quarterly_points_never_overwrites_a_genuine_quarter_with_a_derived_one():
    # If a period end already has an honest, directly-reported
    # standalone-quarter value, a same-end cumulative entry (e.g. a
    # restated or duplicate value from a different filing) must never
    # clobber it.
    entries = [
        _duration("2026-01-01", "2026-03-31", 100, form="10-Q"),
        _duration("2026-01-01", "2026-06-30", 999, form="10-Q"),  # would derive to 899
    ]
    points = sec_edgar._quarterly_points(entries, "duration")
    assert points["2026-06-30"]["val"] == 899  # still correctly derived, nothing to overwrite here
    # Now add an actual directly-reported Q2 value with a later filing
    # date -- it should win outright, not get subtracted from.
    entries.append(_duration("2026-04-01", "2026-06-30", 450, form="10-Q", filed="2099-01-01"))
    points = sec_edgar._quarterly_points(entries, "duration")
    assert points["2026-06-30"]["val"] == 450
    assert "derived" not in points["2026-06-30"]


def test_quarterly_points_leaves_a_period_blank_with_no_earlier_baseline_to_subtract_from():
    # Only a lone 6-month cumulative entry exists, nothing earlier in
    # the same fiscal year -- there's no honest way to know how much of
    # it happened in Q1 vs Q2, so it should stay unfilled rather than
    # being shown as if it were a real Q2-only number.
    entries = [_duration("2026-01-01", "2026-06-30", 24_910_000_000, form="10-Q")]
    points = sec_edgar._quarterly_points(entries, "duration")
    assert points == {}


def test_build_statement_derives_debt_issuance_quarter_from_meta_shaped_cumulative_data(monkeypatch):
    facts = {
        "entityName": "Meta-shaped Corp",
        "facts": {
            "us-gaap": {
                "ProceedsFromIssuanceOfLongTermDebt": {
                    "units": {
                        "USD": [
                            _duration("2025-01-01", "2025-09-30", 0, form="10-Q"),
                            _duration("2025-01-01", "2025-12-31", 29_906_000_000, form="10-K"),
                        ]
                    }
                },
            }
        },
    }
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: facts)
    df = sec_edgar.build_statement("0000000000", "Cash Flow Statement", n_periods=4)
    assert df.loc["Proceeds from debt issuance", "2025-12-31"] == 29_906_000_000


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
    test_working_capital_line_items_flip_sign_for_asset_side_tags_only(_FakeMonkeypatch())
    test_change_in_working_capital_sums_all_six_components(_FakeMonkeypatch())
    test_property_and_equipment_falls_back_to_the_finance_lease_combined_tag(_FakeMonkeypatch())
    test_balance_sheet_total_debt_and_ratios(_FakeMonkeypatch())
    test_field_formats_cover_every_row(_FakeMonkeypatch())
    test_statement_categories_cover_every_field_exactly_once()
    test_every_preset_field_is_a_real_label_somewhere()
    test_full_preset_is_every_field()
    test_preset_default_fields_only_returns_fields_that_statement_has()
    test_preset_default_fields_unknown_preset_defaults_to_everything_on()
    test_quarterly_points_derives_q4_from_9_month_and_full_year_cumulative_totals()
    test_quarterly_points_derives_q2_from_discrete_q1_and_6_month_cumulative()
    test_quarterly_points_never_overwrites_a_genuine_quarter_with_a_derived_one()
    test_quarterly_points_leaves_a_period_blank_with_no_earlier_baseline_to_subtract_from()
    test_build_statement_derives_debt_issuance_quarter_from_meta_shaped_cumulative_data(_FakeMonkeypatch())
    print("All tests passed.")
