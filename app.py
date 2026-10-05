"""
app.py
======
The Streamlit GUI. This file is intentionally "thin" -- it just asks
sec_edgar.py for data and displays it. All the actual logic (talking to
SEC, matching tags, filtering quarters) lives in sec_edgar.py.

To run this app:
    streamlit run app.py

Streamlit re-runs this entire script top-to-bottom every time you
interact with a widget (type in a box, click a button, move a slider).
That feels strange coming from a normal script, but it's the whole
model: your script IS the page, and it just redraws itself. That's why
sec_edgar.py caches SEC's responses to disk -- otherwise every click
would re-download data you already have.
"""

import pandas as pd
import streamlit as st

import sec_edgar

st.set_page_config(page_title="Financial Terminal", layout="wide")

st.title("Financial Terminal")
st.caption("Free financial statement data straight from SEC EDGAR.")

ALL_STATEMENTS = list(sec_edgar.STATEMENTS.keys())
PRESET_LABELS = [preset.label for preset in sec_edgar.PRESETS]


def _checkbox_key(statement: str, label: str) -> str:
    """The st.session_state key one field's checkbox widget uses this
    run, e.g. 'chk__Balance Sheet__Total assets'. This key is NOT where
    toggle state actually lives long-term -- see field_toggles below
    for why."""
    return f"chk__{statement}__{label}"


def _apply_preset():
    """Runs as the Preset selectbox's on_change callback -- which
    Streamlit calls BEFORE it reruns the script and redraws the
    checkboxes below. That ordering is what makes "picking a preset"
    actually check/uncheck boxes: whatever this writes to
    session_state now is what the checkboxes read as their value a
    moment later, when they're instantiated. Setting a widget's
    session_state key AFTER it's already been drawn in the SAME run
    raises an error -- a callback is exactly how you avoid that, since
    it always runs before that redraw.

    A preset can name fields from all three statements at once (e.g.
    Concise includes both Total revenue and Total assets), so this
    updates every statement's remembered toggle state, not just
    whichever one is on screen right now -- switching statements
    afterward still shows the preset applied.
    """
    chosen = st.session_state.preset_choice
    for stmt in sec_edgar.STATEMENTS:
        defaults = sec_edgar.preset_default_fields(chosen, stmt)
        stmt_toggles = st.session_state.field_toggles.setdefault(stmt, {})
        for label in sec_edgar.statement_field_labels(stmt):
            value = label in defaults
            stmt_toggles[label] = value
            # Also overwrite the LIVE widget key, when one already
            # exists, so a statement that's already on screen updates
            # immediately. Streamlit checkboxes always prefer an
            # existing session_state[key] over anything else -- if we
            # only updated field_toggles, a checkbox already drawn for
            # the current statement would keep showing its OLD value
            # until you switched statements and back.
            key = _checkbox_key(stmt, label)
            if key in st.session_state:
                st.session_state[key] = value


with st.sidebar:
    st.header("Look up a company")
    # A second "Fetch data" button, right up top -- functionally
    # identical to the one at the bottom of the sidebar (see
    # fetch_clicked below), just placed here so you don't have to
    # scroll past every field checkbox to re-fetch after changing the
    # ticker. Streamlit requires every widget to have a unique key once
    # there's more than one with the same label, hence key="fetch_top"
    # here and key="fetch_bottom" further down.
    fetch_clicked_top = st.button("Fetch data", type="primary", key="fetch_top")
    ticker = st.text_input("Ticker", value="META").strip().upper()
    statement = st.selectbox("Statement", ALL_STATEMENTS)

    # Tier 4: Annual (10-K) vs. Quarterly (10-Q) -- a straight choice
    # between reported fiscal quarters (the app's original behavior)
    # and reported full fiscal years, not a computed TTM or anything
    # else. sec_edgar.build_statement()'s period_type param does the
    # real work; this radio just picks which string to pass it.
    period_type_choice = st.radio(
        "Period type", ["Quarterly (10-Q)", "Annual (10-K)"], horizontal=True
    )
    period_type = "annual" if period_type_choice.startswith("Annual") else "quarterly"
    n_periods_label = "How many recent fiscal years?" if period_type == "annual" else "How many recent quarters?"
    n_periods = st.slider(n_periods_label, min_value=1, max_value=8, value=4)

    st.header("Fields to show")
    if "preset_choice" not in st.session_state:
        st.session_state.preset_choice = "Full"  # show everything until the user picks otherwise
    if "field_toggles" not in st.session_state:
        # {statement: {label: bool}} -- the DURABLE record of which
        # fields are checked, independent of any single checkbox
        # widget. This matters because Streamlit quietly forgets a
        # widget's session_state value once that widget isn't drawn
        # for a run (e.g. you switch from Balance Sheet to Income
        # Statement, so Balance Sheet's checkboxes don't get created
        # that run) -- without a separate place to remember it, a
        # manual toggle would be lost the moment you looked away from
        # that statement. Every checkbox below reads its default from
        # here and writes back into it immediately after being drawn.
        st.session_state.field_toggles = {}
    st.selectbox("Preset", PRESET_LABELS, key="preset_choice", on_change=_apply_preset)
    st.caption("Pick individual fields below to fine-tune -- switching presets resets them.")

    # Grouped checkboxes, one subheading per category (Assets /
    # Liabilities / Equity for the Balance Sheet, and so on) -- this
    # replaces the old flat "Line items to include" multiselect now
    # that there are ~20-30 fields per statement instead of a handful.
    stmt_toggles = st.session_state.field_toggles.setdefault(statement, {})
    for category, labels in sec_edgar.statement_categories(statement).items():
        st.subheader(category)
        for label in labels:
            key = _checkbox_key(statement, label)
            if key not in st.session_state:
                # First time this widget is being created THIS visit to
                # this statement -- seed it from what we remember for
                # this field (a past manual toggle, or a preset
                # applied while this statement was off screen), falling
                # back to the active preset if we've never seen this
                # field/statement combo before at all.
                remembered = stmt_toggles.get(label)
                if remembered is None:
                    remembered = label in sec_edgar.preset_default_fields(
                        st.session_state.preset_choice, statement
                    )
                st.session_state[key] = remembered
            checked = st.checkbox(label, key=key)
            stmt_toggles[label] = checked  # keep the durable record current

    fetch_clicked_bottom = st.button("Fetch data", type="primary", key="fetch_bottom")
    # Either button firing this run should trigger the same fetch below.
    fetch_clicked = fetch_clicked_top or fetch_clicked_bottom

if "df" not in st.session_state:
    st.session_state.df = None
    st.session_state.company = None

# Two tabs share this one page: Single Company (everything above, driven
# by the sidebar) and Comparables Analysis (Tier 3 -- its own ticker
# list and controls live in its own tab body, not the sidebar, since
# it isn't tied to the single "Ticker" box above at all).
tab_single, tab_comparables = st.tabs(["Single Company", "Comparables Analysis"])

with tab_single:
    if fetch_clicked:
        with st.spinner(f"Looking up {ticker} on SEC EDGAR..."):
            try:
                cik = sec_edgar.get_cik_for_ticker(ticker)
                name = sec_edgar.company_name(cik)
                df = sec_edgar.build_statement(cik, statement, n_periods=n_periods, period_type=period_type)
                if df.empty or len(df.columns) == 0:
                    # Most likely: Annual was picked for a company with
                    # no 10-K data under any of this statement's tags
                    # (a very recent IPO, say) -- a blank table with no
                    # explanation would look like a bug, not a "there's
                    # genuinely nothing here" result.
                    st.session_state.df = None
                    period_word = "annual (10-K)" if period_type == "annual" else "quarterly (10-Q)"
                    st.warning(
                        f"No {period_word} data found for {ticker} in {statement}. "
                        + ("Try Quarterly instead." if period_type == "annual" else "Try Annual instead.")
                    )
                else:
                    st.session_state.df = df
                    st.session_state.company = f"{name} ({ticker}) -- CIK {cik}"
                    st.session_state.statement = statement
                    st.session_state.ticker = ticker
                    st.session_state.period_type = period_type
            except sec_edgar.SecEdgarError as e:
                st.session_state.df = None
                st.error(str(e))

    if st.session_state.df is not None:
        df = st.session_state.df
        shown_statement = st.session_state.statement
        shown_period_type = st.session_state.get("period_type", "quarterly")
        period_suffix = " (Annual, 10-K)" if shown_period_type == "annual" else ""
        st.subheader(f"{shown_statement} -- {st.session_state.company}{period_suffix}")

        # Format each row according to what kind of number it holds --
        # dollar figures get comma separators, margins print as a percent,
        # and ratios (like Debt/equity) print as a plain decimal. Without
        # this, a 34.5% margin would get squashed to "0" by the old
        # dollar-only formatting (f"{0.345:,.0f}" rounds to "0"). Always use
        # shown_statement (the statement THIS data was fetched for) here,
        # not the live sidebar dropdown -- if you've since switched
        # statements without re-fetching, those would disagree and every
        # format would silently fall back to "dollar".
        formats = sec_edgar.field_formats(shown_statement)

        def format_value(label: str, v: float) -> str:
            if pd.isnull(v):
                return "—"
            kind = formats.get(label, "dollar")
            if kind == "pct":
                return f"{v:.1%}"
            if kind == "ratio":
                return f"{v:.2f}"
            return f"{v:,.0f}"

        display_df = df.astype(object)
        for label in display_df.index:
            for col in display_df.columns:
                display_df.loc[label, col] = format_value(label, df.loc[label, col])

        # Which rows to show/export comes from field_toggles for
        # shown_statement -- the durable record, not a live checkbox
        # widget key, for the same reason formats above uses
        # shown_statement: if you've switched the sidebar to a different
        # statement without re-fetching, shown_statement's checkboxes
        # might not even be on screen (or drawn) this run.
        shown_toggles = st.session_state.field_toggles.get(shown_statement, {})
        selected_rows = [label for label in df.index if shown_toggles.get(label, True)]

        if not selected_rows:
            st.warning("Every field is unchecked in the sidebar -- check at least one to see the table.")
        else:
            st.dataframe(display_df.loc[selected_rows], use_container_width=True)

            csv_bytes = df.loc[selected_rows].to_csv().encode("utf-8")
            st.download_button(
                label="Export selected rows to CSV",
                data=csv_bytes,
                file_name=f"{st.session_state.ticker}_{shown_statement.replace(' ', '_').lower()}.csv",
                mime="text/csv",
            )
    else:
        st.info("Enter a ticker in the sidebar and click **Fetch data** to get started. Try META, AAPL, or MSFT.")

with tab_comparables:
    st.subheader("Comparables Analysis")
    st.caption(
        "Price, Market Cap and Beta come from Finviz (finvizfinance) -- an unofficial, "
        "free source, since SEC EDGAR only has filed financial statements, not live "
        "market data. Sales / EBITDA / EBIT / Earnings are trailing-twelve-months (TTM), "
        "the sum of each company's own most recently reported four quarters."
    )

    comps_tickers_input = st.text_input(
        "Tickers (comma-separated, up to 10)",
        value="KO, PEP, KDP, MNST, FIZZ",
        key="comps_tickers_input",
    )
    run_comps_clicked = st.button("Run Comparables Analysis", type="primary")

    if "comps_df" not in st.session_state:
        st.session_state.comps_df = None
        st.session_state.comps_errors = {}

    if run_comps_clicked:
        requested_tickers = [t.strip().upper() for t in comps_tickers_input.split(",") if t.strip()]
        if len(requested_tickers) > sec_edgar.MAX_COMPARABLES_TICKERS:
            st.error(
                f"That's {len(requested_tickers)} tickers -- Comparables Analysis supports at "
                f"most {sec_edgar.MAX_COMPARABLES_TICKERS} at a time. Trim the list and try again."
            )
        elif not requested_tickers:
            st.error("Enter at least one ticker.")
        else:
            # Sequential fetch with a progress bar -- each ticker means
            # several SEC EDGAR calls plus one Finviz lookup, so this can
            # take a few seconds per ticker; the progress bar is here so
            # ten tickers doesn't look like the app has frozen.
            progress = st.progress(0.0, text="Starting...")
            comps_df, comps_errors = None, {}
            try:
                # build_comparables_table() itself is sequential internally,
                # so we can't show live per-ticker progress from inside it
                # without restructuring it to a generator -- for now the bar
                # advances in two steps (start, done) rather than per-ticker.
                # A per-ticker progress callback is a reasonable follow-up if
                # ten-ticker runs feel slow enough to want finer feedback.
                progress.progress(0.2, text=f"Looking up {len(requested_tickers)} tickers...")
                comps_df, comps_errors = sec_edgar.build_comparables_table(requested_tickers)
                progress.progress(1.0, text="Done.")
            except sec_edgar.SecEdgarError as e:
                st.error(str(e))
            finally:
                progress.empty()
            st.session_state.comps_df = comps_df
            st.session_state.comps_errors = comps_errors

    if st.session_state.comps_errors:
        for bad_ticker, reason in st.session_state.comps_errors.items():
            st.warning(f"Skipped **{bad_ticker}**: {reason}")

    if st.session_state.comps_df is not None and not st.session_state.comps_df.empty:
        comps_df = st.session_state.comps_df
        comps_formats = sec_edgar.comparables_field_formats()

        def format_comps_value(kind: str, v: float) -> str:
            if pd.isnull(v):
                return "—"
            if kind == "price":
                return f"${v:,.2f}"
            if kind == "dollar_m":
                return f"{v / 1_000_000:,.0f}"
            if kind == "beta":
                return f"{v:.2f}"
            if kind == "multiple":
                return f"{v:.1f}x"
            return f"{v:,.0f}"

        comps_display = comps_df.astype(object)
        for col in comps_display.columns:
            kind = comps_formats.get(col, "dollar_m")
            for label in comps_display.index:
                comps_display.loc[label, col] = format_comps_value(kind, comps_df.loc[label, col])

        st.dataframe(comps_display, use_container_width=True)
        st.caption(
            "$ in millions except Price and per-share figures. "
            "* = most recently reported period is a full fiscal year (10-K), not a single quarter -- "
            "see TAG_COVERAGE_GUIDE.md / Charter Section 5."
        )

        csv_bytes = comps_df.to_csv().encode("utf-8")
        st.download_button(
            label="Export comparables table to CSV",
            data=csv_bytes,
            file_name="comparables_analysis.csv",
            mime="text/csv",
        )
    elif run_comps_clicked and not st.session_state.comps_errors:
        st.warning("No data came back for any of those tickers.")
