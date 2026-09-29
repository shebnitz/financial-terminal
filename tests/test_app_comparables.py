"""
Tests for the Comparables Analysis tab in app.py (Tier 3), using the same
AppTest harness as test_app.py -- runs the real app.py headlessly and
clicks its actual widgets, with sec_edgar.build_comparables_table()
monkeypatched so nothing here touches SEC or Finviz.

Run with:  python -m pytest tests/test_app_comparables.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def _fake_comps_df():
    return pd.DataFrame(
        {
            "Price": [38.14, 81.37],
            "Market Cap": [168_041_000_000, 123_883_000_000],
            "EV": [185_122_000_000, 143_824_000_000],
            "Sales": [46_854_000_000, 66_415_000_000],
            "EBITDA": [13_104_000_000, 12_344_000_000],
            "EBIT": [11_127_000_000, 9_878_000_000],
            "Earnings": [7_381_000_000, 5_618_000_000],
            "Beta": [0.62, 0.55],
            "EV/Sales": [4.0, 2.2],
            "EV/EBITDA": [14.1, 11.7],
            "EV/EBIT": [16.6, 14.6],
            "P/E": [22.8, 22.1],
        },
        index=["The Coca-Cola Company", "Pepsico, Inc."],
    )


def test_comparables_tab_shows_up_alongside_single_company():
    at = AppTest.from_file(APP_PATH).run()
    assert not at.exception
    tab_labels = [t.value for t in at.tabs] if hasattr(at.tabs[0], "value") else None
    # Whatever AppTest's exact tab introspection looks like, the button
    # and text input this tab defines should exist somewhere in the tree.
    assert any(ti.key == "comps_tickers_input" for ti in at.text_input)
    assert any(b.label == "Run Comparables Analysis" for b in at.button)


def test_running_comparables_analysis_shows_a_formatted_table_and_csv_button(monkeypatch):
    import sec_edgar

    monkeypatch.setattr(sec_edgar, "build_comparables_table", lambda tickers: (_fake_comps_df(), {}))

    at = AppTest.from_file(APP_PATH).run()
    at.text_input(key="comps_tickers_input").set_value("KO, PEP").run()
    [b for b in at.button if b.label == "Run Comparables Analysis"][0].click().run()
    assert not at.exception, at.exception

    # Index directly into the rendered DataFrame rather than checking a
    # printed str() of it -- pandas truncates wide tables with "..." in
    # their string repr, which would make this test pass or fail based
    # on column count rather than on what actually got formatted.
    shown = at.dataframe[-1].value
    assert shown.loc["The Coca-Cola Company", "EV/Sales"] == "4.0x"       # 'x' suffix on multiples
    assert shown.loc["The Coca-Cola Company", "Sales"] == "46,854"        # $ in millions, comma-separated
    assert shown.loc["The Coca-Cola Company", "Price"] == "$38.14"        # dollars-and-cents
    assert shown.loc["The Coca-Cola Company", "Beta"] == "0.62"           # two decimals, no 'x' suffix

    assert any(b.label == "Export comparables table to CSV" for b in at.download_button)


def test_a_failing_ticker_shows_a_warning_but_does_not_crash(monkeypatch):
    import sec_edgar

    def _fake(tickers):
        df = _fake_comps_df().loc[["The Coca-Cola Company"]]
        return df, {"NOPE": "Couldn't find ticker 'NOPE' in SEC's company list."}

    monkeypatch.setattr(sec_edgar, "build_comparables_table", _fake)

    at = AppTest.from_file(APP_PATH).run()
    at.text_input(key="comps_tickers_input").set_value("KO, NOPE").run()
    [b for b in at.button if b.label == "Run Comparables Analysis"][0].click().run()
    assert not at.exception, at.exception

    assert any("NOPE" in w.value for w in at.warning)
    shown = at.dataframe[-1].value
    assert shown.loc["The Coca-Cola Company", "Sales"] == "46,854"  # the good ticker's row still rendered


def test_more_than_ten_tickers_is_rejected_before_calling_sec_edgar(monkeypatch):
    import sec_edgar

    calls = []
    monkeypatch.setattr(
        sec_edgar,
        "build_comparables_table",
        lambda tickers: calls.append(tickers) or (None, {}),
    )

    at = AppTest.from_file(APP_PATH).run()
    eleven = ", ".join(f"T{i}" for i in range(11))
    at.text_input(key="comps_tickers_input").set_value(eleven).run()
    [b for b in at.button if b.label == "Run Comparables Analysis"][0].click().run()
    assert not at.exception, at.exception

    assert calls == []  # never even reached sec_edgar -- rejected client-side first
    assert any("10" in e.value for e in at.error)
