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

with st.sidebar:
    st.header("Look up a company")
    ticker = st.text_input("Ticker", value="META").strip().upper()
    statement = st.selectbox("Statement", list(sec_edgar.STATEMENTS.keys()))
    n_periods = st.slider("How many recent quarters?", min_value=1, max_value=8, value=4)
    fetch_clicked = st.button("Fetch data", type="primary")

if "df" not in st.session_state:
    st.session_state.df = None
    st.session_state.company = None

if fetch_clicked:
    with st.spinner(f"Looking up {ticker} on SEC EDGAR..."):
        try:
            cik = sec_edgar.get_cik_for_ticker(ticker)
            name = sec_edgar.company_name(cik)
            df = sec_edgar.build_statement(cik, statement, n_periods=n_periods)
            st.session_state.df = df
            st.session_state.company = f"{name} ({ticker}) -- CIK {cik}"
            st.session_state.statement = statement
        except sec_edgar.SecEdgarError as e:
            st.session_state.df = None
            st.error(str(e))

if st.session_state.df is not None:
    df = st.session_state.df
    st.subheader(f"{st.session_state.statement} -- {st.session_state.company}")

    # Format big dollar numbers with commas so the table is readable.
    display_df = df.copy()
    for col in display_df.columns:
        display_df[col] = display_df[col].apply(lambda v: f"{v:,.0f}" if pd.notnull(v) else "—")

    # Let the user pick which line items ("some or all") to view/export.
    all_rows = list(df.index)
    selected_rows = st.multiselect("Line items to include", all_rows, default=all_rows)

    st.dataframe(display_df.loc[selected_rows], use_container_width=True)

    csv_bytes = df.loc[selected_rows].to_csv().encode("utf-8")
    st.download_button(
        label="Export selected rows to CSV",
        data=csv_bytes,
        file_name=f"{ticker}_{statement.replace(' ', '_').lower()}.csv",
        mime="text/csv",
    )
else:
    st.info("Enter a ticker in the sidebar and click **Fetch data** to get started. Try META, AAPL, or MSFT.")
