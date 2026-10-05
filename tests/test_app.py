"""
Tests for app.py's field-toggle/preset UI (Tier 2), using Streamlit's
built-in AppTest harness -- it runs the actual app.py script (top to
bottom, the same way `streamlit run app.py` would) headlessly, so these
exercise the real preset selectbox, checkboxes, and Fetch button the
same way clicking them in a browser would, without needing a browser or
real SEC network access.

Run with:  python -m pytest tests/test_app.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def _statement_selectbox(at):
    return [sb for sb in at.sidebar.selectbox if sb.label == "Statement"][0]


def test_full_preset_checks_every_field_by_default():
    at = AppTest.from_file(APP_PATH).run()
    assert not at.exception
    assert at.selectbox(key="preset_choice").value == "Full"
    assert all(cb.value for cb in at.sidebar.checkbox)


def test_concise_preset_only_checks_its_own_fields():
    at = AppTest.from_file(APP_PATH).run()
    at.selectbox(key="preset_choice").select("Concise").run()
    assert not at.exception

    checked = {cb.label: cb.value for cb in at.sidebar.checkbox}
    assert checked["Total assets"] is True
    assert checked["Total debt"] is True
    assert checked["Goodwill"] is False  # not part of Concise


def test_preset_follows_statement_switch_without_reselecting():
    # Concise includes fields from all three statements at once (Total
    # assets is Balance Sheet, Total revenue is Income Statement, ...).
    # Switching which statement is showing shouldn't require picking
    # the preset again to see it applied there too.
    at = AppTest.from_file(APP_PATH).run()
    at.selectbox(key="preset_choice").select("Concise").run()
    _statement_selectbox(at).select("Income Statement").run()
    assert not at.exception

    checked = {cb.label: cb.value for cb in at.sidebar.checkbox}
    assert checked["Total revenue"] is True
    assert checked["Operating income"] is True
    assert checked["Research and development"] is False  # not part of Concise


def test_manual_toggle_persists_across_a_statement_round_trip():
    # State is "keyed per statement" per the charter -- a manual
    # checkbox change on the Balance Sheet should survive visiting
    # Income Statement and coming back, not get reset.
    at = AppTest.from_file(APP_PATH).run()
    at.selectbox(key="preset_choice").select("Concise").run()
    goodwill = [cb for cb in at.sidebar.checkbox if cb.label == "Goodwill"][0]
    goodwill.check().run()

    _statement_selectbox(at).select("Income Statement").run()
    _statement_selectbox(at).select("Balance Sheet").run()

    checked = {cb.label: cb.value for cb in at.sidebar.checkbox}
    assert checked["Goodwill"] is True


def test_switching_presets_discards_the_earlier_manual_toggle():
    # Per the charter's interaction model: picking a preset resets that
    # statement's checkboxes to the preset's defaults, discarding any
    # manual tweaks -- simple and predictable, even if it means losing
    # the earlier manual change.
    at = AppTest.from_file(APP_PATH).run()
    at.selectbox(key="preset_choice").select("Concise").run()
    goodwill = [cb for cb in at.sidebar.checkbox if cb.label == "Goodwill"][0]
    goodwill.check().run()  # manual tweak on top of Concise

    at.selectbox(key="preset_choice").select("Leverage Mode").run()
    checked = {cb.label: cb.value for cb in at.sidebar.checkbox}
    assert checked["Goodwill"] is False  # back to Leverage Mode's own default


def test_fetched_table_only_shows_checked_fields_with_correct_formatting(monkeypatch):
    import sec_edgar

    fake_df = pd.DataFrame(
        {"2026-06-30": [60801.0, 18775.0, 0.309]},
        index=["Total revenue", "Operating income", "Operating margin %"],
    )
    monkeypatch.setattr(sec_edgar, "build_statement", lambda cik, statement, n_periods=4, period_type="quarterly": fake_df)
    monkeypatch.setattr(sec_edgar, "get_cik_for_ticker", lambda ticker: "0001326801")
    monkeypatch.setattr(sec_edgar, "company_name", lambda cik: "Meta Platforms, Inc.")

    at = AppTest.from_file(APP_PATH).run()
    at.selectbox(key="preset_choice").select("Profitability Mode").run()
    _statement_selectbox(at).select("Income Statement").run()
    [b for b in at.sidebar.button if b.label == "Fetch data"][0].click().run()
    assert not at.exception, at.exception

    table_text = str(at.dataframe[0].value)
    assert "60,801" in table_text  # dollar formatting, unaffected by the % row also on screen
    assert "30.9%" in table_text  # pct formatting -- this is the bug fixed earlier in Tier 1


def test_top_fetch_button_works_the_same_as_the_bottom_one(monkeypatch):
    # There are two "Fetch data" buttons now -- one right under "Look up
    # a company" (key="fetch_top") so you don't have to scroll past
    # every field checkbox to re-fetch, and the original one at the
    # bottom of the sidebar (key="fetch_bottom"). Both should trigger
    # the exact same fetch.
    import sec_edgar

    fake_df = pd.DataFrame({"2026-06-30": [449956000000.0]}, index=["Total assets"])
    monkeypatch.setattr(sec_edgar, "build_statement", lambda cik, statement, n_periods=4, period_type="quarterly": fake_df)
    monkeypatch.setattr(sec_edgar, "get_cik_for_ticker", lambda ticker: "0001326801")
    monkeypatch.setattr(sec_edgar, "company_name", lambda cik: "Meta Platforms, Inc.")

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="fetch_top").click().run()
    assert not at.exception, at.exception
    assert "449,956,000,000" in str(at.dataframe[0].value)
    # Confirms the fetch actually populated the page (not just "didn't crash").
    assert any("Meta Platforms" in s.value for s in at.subheader)


def test_annual_period_type_is_passed_through_and_shown_in_the_subheader(monkeypatch):
    # Tier 4: the "Period type" radio defaults to Quarterly; picking
    # Annual (10-K) should pass period_type="annual" into
    # build_statement() and label the results as annual in the subheader.
    import sec_edgar

    fake_annual_df = pd.DataFrame({"2025-12-31": [449956000000.0]}, index=["Total assets"])
    captured_calls = []

    def fake_build_statement(cik, statement, n_periods=4, period_type="quarterly"):
        captured_calls.append(period_type)
        return fake_annual_df

    monkeypatch.setattr(sec_edgar, "build_statement", fake_build_statement)
    monkeypatch.setattr(sec_edgar, "get_cik_for_ticker", lambda ticker: "0001326801")
    monkeypatch.setattr(sec_edgar, "company_name", lambda cik: "Meta Platforms, Inc.")

    at = AppTest.from_file(APP_PATH).run()
    [r for r in at.sidebar.radio if r.label == "Period type"][0].set_value("Annual (10-K)").run()
    [b for b in at.sidebar.button if b.label == "Fetch data"][0].click().run()
    assert not at.exception, at.exception

    assert captured_calls == ["annual"]
    assert any("(Annual, 10-K)" in s.value for s in at.subheader)


def test_quarterly_is_the_default_period_type(monkeypatch):
    import sec_edgar

    fake_df = pd.DataFrame({"2026-06-30": [449956000000.0]}, index=["Total assets"])
    captured_calls = []

    def fake_build_statement(cik, statement, n_periods=4, period_type="quarterly"):
        captured_calls.append(period_type)
        return fake_df

    monkeypatch.setattr(sec_edgar, "build_statement", fake_build_statement)
    monkeypatch.setattr(sec_edgar, "get_cik_for_ticker", lambda ticker: "0001326801")
    monkeypatch.setattr(sec_edgar, "company_name", lambda cik: "Meta Platforms, Inc.")

    at = AppTest.from_file(APP_PATH).run()
    [b for b in at.sidebar.button if b.label == "Fetch data"][0].click().run()
    assert not at.exception, at.exception

    assert captured_calls == ["quarterly"]
    assert not any("(Annual, 10-K)" in s.value for s in at.subheader)


def test_annual_mode_with_no_data_shows_a_friendly_warning_not_an_empty_table(monkeypatch):
    import sec_edgar

    empty_df = pd.DataFrame()  # what build_statement returns when nothing matched

    monkeypatch.setattr(sec_edgar, "build_statement", lambda cik, statement, n_periods=4, period_type="quarterly": empty_df)
    monkeypatch.setattr(sec_edgar, "get_cik_for_ticker", lambda ticker: "0001326801")
    monkeypatch.setattr(sec_edgar, "company_name", lambda cik: "Meta Platforms, Inc.")

    at = AppTest.from_file(APP_PATH).run()
    [r for r in at.sidebar.radio if r.label == "Period type"][0].set_value("Annual (10-K)").run()
    [b for b in at.sidebar.button if b.label == "Fetch data"][0].click().run()
    assert not at.exception, at.exception

    assert any("No annual (10-K) data found" in w.value for w in at.warning)
    assert len(at.dataframe) == 0  # no table rendered at all, not a blank one


def test_unchecking_every_field_shows_a_warning_not_a_crash(monkeypatch):
    import sec_edgar

    fake_df = pd.DataFrame({"2026-06-30": [449956000000.0]}, index=["Total assets"])
    monkeypatch.setattr(sec_edgar, "build_statement", lambda cik, statement, n_periods=4, period_type="quarterly": fake_df)
    monkeypatch.setattr(sec_edgar, "get_cik_for_ticker", lambda ticker: "0001326801")
    monkeypatch.setattr(sec_edgar, "company_name", lambda cik: "Meta Platforms, Inc.")

    at = AppTest.from_file(APP_PATH).run()
    for cb in at.sidebar.checkbox:
        cb.uncheck()
    at.run()
    [b for b in at.sidebar.button if b.label == "Fetch data"][0].click().run()
    assert not at.exception, at.exception
    assert any("unchecked" in w.value for w in at.warning)
