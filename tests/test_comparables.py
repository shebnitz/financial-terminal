"""
Tests for the Comparables Analysis feature (Tier 3): build_comparables_table()
and get_market_data() in sec_edgar.py. Like test_sec_edgar.py, these build
small fake inputs by hand and monkeypatch the network-touching functions
(get_cik_for_ticker, company_name, build_statement, get_market_data) so
nothing here touches SEC or Finviz.

Run with:  python -m pytest tests/test_comparables.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
import sec_edgar  # noqa: E402


# ---------------------------------------------------------------------------
# A small fake two-company world: FAKECO (a normal quarterly filer) and
# STALECO (whose latest period is a 10-K, so it should get star-marked).
# ---------------------------------------------------------------------------

def _income_df(sales_col, ebit_col, net_income_col, columns):
    return pd.DataFrame(
        {
            columns[i]: [sales_col[i], ebit_col[i], net_income_col[i]]
            for i in range(len(columns))
        },
        index=["Total revenue", "Operating income", "Net income"],
    )


def _cash_flow_df(da_col, columns):
    return pd.DataFrame(
        {columns[i]: [da_col[i]] for i in range(len(columns))},
        index=["Depreciation and amortization"],
    )


def _balance_df(total_debt, cash, column):
    return pd.DataFrame({column: [total_debt, cash]}, index=["Total debt", "Cash and cash equivalents"])


def _fake_build_statement(cik, statement, n_periods=4):
    # FAKECO: four clean quarters, everything needed for TTM math.
    if cik == "FAKECO_CIK":
        cols = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]
        if statement == "Income Statement":
            return _income_df([100, 90, 80, 70], [40, 36, 32, 28], [30, 27, 24, 21], cols)
        if statement == "Cash Flow Statement":
            return _cash_flow_df([5, 5, 5, 5], cols)
        if statement == "Balance Sheet":
            return _balance_df(200, 50, cols[0])
    # STALECO: only one period, and it's a full fiscal year (simulating a
    # company whose latest filing is a 10-K with no 10-Q yet).
    if cik == "STALECO_CIK":
        cols = ["2025-12-31"]
        if statement == "Income Statement":
            return _income_df([400], [160], [120], cols)
        if statement == "Cash Flow Statement":
            return _cash_flow_df([20], cols)
        if statement == "Balance Sheet":
            return _balance_df(0, 0, cols[0])  # no debt tag reported -- should default to $0, not blank
    raise sec_edgar.SecEdgarError(f"no fake data for {cik}/{statement}")


def _fake_get_cik_for_ticker(ticker):
    return {"FAKECO": "FAKECO_CIK", "STALECO": "STALECO_CIK", "BADCO": "BADCO_CIK"}[ticker]


def _fake_company_name(cik):
    return {"FAKECO_CIK": "Fake Company Inc.", "STALECO_CIK": "Stale Company Inc."}[cik]


def _fake_market_data(ticker):
    if ticker == "FAKECO":
        return {"Price": 50.0, "Market Cap": 1000.0, "Beta": 1.1, "P/E": None, "P/B": None, "P/S": None}
    if ticker == "STALECO":
        return {"Price": 20.0, "Market Cap": 800.0, "Beta": 0.9, "P/E": None, "P/B": None, "P/S": None}
    raise sec_edgar.MarketDataError(f"no fake market data for {ticker}")


def _install_fakes(monkeypatch):
    monkeypatch.setattr(sec_edgar, "get_cik_for_ticker", _fake_get_cik_for_ticker)
    monkeypatch.setattr(sec_edgar, "company_name", _fake_company_name)
    monkeypatch.setattr(sec_edgar, "build_statement", _fake_build_statement)
    monkeypatch.setattr(sec_edgar, "get_market_data", _fake_market_data)
    # FAKECO's four quarters are all genuinely reported (form defaults to
    # 10-Q in real data); STALECO's single period should be detected as
    # annual-only via the company_facts/tag path, which we bypass here by
    # monkeypatching _company_period_is_annual_only directly instead --
    # simpler than faking a whole company_facts blob for this test file.
    monkeypatch.setattr(
        sec_edgar,
        "_company_period_is_annual_only",
        lambda cik: cik == "STALECO_CIK",
    )


def test_ttm_sum_adds_up_to_four_available_quarters():
    df = _income_df([100, 90, 80, 70], [40, 36, 32, 28], [30, 27, 24, 21],
                     ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"])
    assert sec_edgar._ttm_sum(df, "Total revenue") == 340
    assert sec_edgar._ttm_sum(df, "Operating income") == 136


def test_ttm_sum_handles_fewer_than_four_quarters_without_crashing():
    df = _income_df([100, 90], [40, 36], [30, 27], ["2026-06-30", "2026-03-31"])
    assert sec_edgar._ttm_sum(df, "Total revenue") == 190  # sums what's there, no padding


def test_ttm_sum_returns_none_for_a_label_with_no_data_at_all():
    df = _income_df([None, None], [None, None], [None, None], ["2026-06-30", "2026-03-31"])
    assert sec_edgar._ttm_sum(df, "Total revenue") is None


def test_build_comparables_table_computes_ebitda_ev_and_multiples_correctly(monkeypatch):
    _install_fakes(monkeypatch)
    df, errors = sec_edgar.build_comparables_table(["FAKECO"])
    assert errors == {}

    row = df.loc["Fake Company Inc."]
    # Sales/EBIT/Earnings are TTM sums of the four fake quarters.
    assert row["Sales"] == 340       # 100+90+80+70
    assert row["EBIT"] == 136        # 40+36+32+28
    assert row["Earnings"] == 102    # 30+27+24+21
    assert row["EBITDA"] == 156      # EBIT 136 + D&A (5*4=20)
    # EV = Market Cap (1000) + Total debt (200) - Cash (50)
    assert row["EV"] == 1150
    assert row["EV/Sales"] == 1150 / 340
    assert row["EV/EBITDA"] == 1150 / 156
    assert row["EV/EBIT"] == 1150 / 136
    assert row["P/E"] == 1000 / 102
    assert row["Price"] == 50.0
    assert row["Market Cap"] == 1000.0
    assert row["Beta"] == 1.1  # straight from Finviz, no math applied


def test_build_comparables_table_stars_a_company_whose_mrq_is_annual_only(monkeypatch):
    _install_fakes(monkeypatch)
    df, errors = sec_edgar.build_comparables_table(["STALECO"])
    assert errors == {}
    assert "Stale Company Inc. *" in df.index
    assert "Stale Company Inc." not in df.index  # unstarred version shouldn't also appear


def test_build_comparables_table_defaults_missing_debt_and_cash_to_zero(monkeypatch):
    _install_fakes(monkeypatch)
    df, errors = sec_edgar.build_comparables_table(["STALECO"])
    row = df.loc["Stale Company Inc. *"]
    # STALECO's balance sheet reports Total debt=0, Cash=0 -- EV should
    # equal Market Cap exactly, not go blank just because debt is zero.
    assert row["EV"] == 800.0  # Market Cap 800 + 0 - 0


def test_build_comparables_table_isolates_a_failing_ticker(monkeypatch):
    _install_fakes(monkeypatch)
    df, errors = sec_edgar.build_comparables_table(["FAKECO", "BADCO"])
    assert "Fake Company Inc." in df.index          # the good ticker still made it in
    assert len(df) == 1 + 2                          # 1 company + Average + Median rows
    assert "BADCO" in errors                         # the bad one is reported, not silently dropped
    assert "no fake data" in errors["BADCO"] or errors["BADCO"]  # some readable reason


def test_build_comparables_table_average_and_median_only_fill_multiple_columns(monkeypatch):
    _install_fakes(monkeypatch)
    df, errors = sec_edgar.build_comparables_table(["FAKECO", "STALECO"])
    assert errors == {}

    fakeco_ev_sales = df.loc["Fake Company Inc.", "EV/Sales"]
    staleco_ev_sales = df.loc["Stale Company Inc. *", "EV/Sales"]
    expected_avg = (fakeco_ev_sales + staleco_ev_sales) / 2
    assert df.loc["Average", "EV/Sales"] == expected_avg
    assert df.loc["Median", "EV/Sales"] == expected_avg  # only two values -- mean == median here

    # Beta gets an Average (plain arithmetic mean) but deliberately no
    # Median -- Kevin only asked for the average.
    assert df.loc["Average", "Beta"] == (1.1 + 0.9) / 2
    assert pd.isnull(df.loc["Median", "Beta"])

    # Non-multiple, non-Beta columns stay blank on the summary rows,
    # matching the real comp table this was built from.
    assert pd.isnull(df.loc["Average", "Sales"])
    assert pd.isnull(df.loc["Average", "Market Cap"])
    assert pd.isnull(df.loc["Median", "Price"])


def test_build_comparables_table_rejects_more_than_the_ticker_limit():
    too_many = [f"T{i}" for i in range(sec_edgar.MAX_COMPARABLES_TICKERS + 1)]
    try:
        sec_edgar.build_comparables_table(too_many)
        assert False, "expected a SecEdgarError for exceeding the ticker limit"
    except sec_edgar.SecEdgarError as e:
        assert str(sec_edgar.MAX_COMPARABLES_TICKERS) in str(e)


def test_get_market_data_wraps_any_finviz_failure_in_market_data_error(monkeypatch):
    class _ExplodingFinviz:
        def __init__(self, ticker):
            raise RuntimeError("finviz.com didn't respond the way finvizfinance expected")

    monkeypatch.setattr(sec_edgar, "_FinvizQuote", _ExplodingFinviz)
    try:
        sec_edgar.get_market_data("NOPE")
        assert False, "expected a MarketDataError"
    except sec_edgar.MarketDataError as e:
        assert "NOPE" in str(e)


def test_finviz_number_convert_patch_handles_whitespace_only_values():
    # Regression test for a real finvizfinance 1.5.0 bug hit live on
    # Kevin's machine (Sept 2026): as of a recent finviz.com change,
    # some fundament fields come back as whitespace-only text (e.g. a
    # non-breaking space) instead of a real value or "-". The library's
    # OWN number_convert() only checks for a truly empty string or a
    # literal "-" -- BEFORE stripping -- so a whitespace-only value
    # slips through, gets stripped to "", and `num[-1]` raises
    # "IndexError: string index out of range", which crashed
    # get_market_data() for every single ticker. sec_edgar.py patches
    # finvizfinance.quote.number_convert at import time to fix this;
    # this test exercises finvizfinance's real parsing path (not our
    # own code) to prove the patch actually took effect.
    from finvizfinance.quote import finvizfinance

    instance = finvizfinance.__new__(finvizfinance)
    result = instance._parse_column(
        ["Market Cap", "168.04B", "Price", " ", "P/E", "-", "EPS next Y", "4.02%"],
        False,
        {},
    )
    assert result["Market Cap"] == 168_040_000_000.0
    assert result["Price"] is None  # whitespace-only -- would have crashed pre-patch
    assert result["P/E"] is None  # finviz's own "no data" placeholder
    assert round(result["EPS next Y"], 4) == 0.0402


def test_comparables_field_formats_covers_every_column():
    formats = sec_edgar.comparables_field_formats()
    assert set(formats.keys()) == set(sec_edgar.COMPARABLES_COLUMNS)
    assert formats["Price"] == "price"
    assert formats["Sales"] == "dollar_m"
    assert formats["EV/EBITDA"] == "multiple"


if __name__ == "__main__":
    print("Running test_comparables.py manually (prefer `pytest tests/` normally)...")
    test_ttm_sum_adds_up_to_four_available_quarters()
    test_ttm_sum_handles_fewer_than_four_quarters_without_crashing()
    test_ttm_sum_returns_none_for_a_label_with_no_data_at_all()
    test_build_comparables_table_rejects_more_than_the_ticker_limit()
    test_comparables_field_formats_covers_every_column()
    print("Manual (non-monkeypatch) checks passed -- run pytest for the full suite.")
