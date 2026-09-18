# Financial Terminal

A small Python + Streamlit app that pulls real financial statement data
(Balance Sheet, Income Statement, Cash Flow Statement) straight from
**SEC EDGAR** -- the SEC's free, public, no-API-key-required data feed
-- and lets you view and export it as CSV.

If you're following along as a Python beginner, start with
**[SETUP_GUIDE.md](SETUP_GUIDE.md)** instead of this file -- it walks
through installing everything, running this app for the first time, and
getting it onto GitHub, step by step.

## What's here

```
financial-terminal/
├── app.py              <- the Streamlit GUI (the only file with UI code)
├── sec_edgar.py         <- the data layer: talks to SEC, builds tables
├── config.py             <- reads your contact info from .env
├── tests/
│   └── test_sec_edgar.py  <- offline tests for the tricky parsing logic
├── requirements.txt      <- the Python packages this project needs
├── .env.example           <- template for your local contact info
└── .gitignore
```

**Why split `app.py` and `sec_edgar.py`?** This is a habit worth
building early: keep "get the data" separate from "show the data."
`sec_edgar.py` doesn't know Streamlit exists. That means you can test it
on its own (`python sec_edgar.py META` or the pytest suite), and if you
ever want a different GUI, a command-line report, or a script that
emails you a daily update, you'd write a new thin file on top of the
same `sec_edgar.py` -- no data logic to redo.

## Running it

```bash
streamlit run app.py
```

Then open the local URL it prints (usually `http://localhost:8501`).
Enter a ticker (try `META`, `AAPL`, or `MSFT`), pick a statement, and
click **Fetch data**.

## How the data actually works

- SEC EDGAR assigns every public company a **CIK** (Central Index Key).
  `sec_edgar.get_cik_for_ticker("META")` looks that up from SEC's own
  ticker list.
- SEC's **company facts API** returns every number a company has ever
  reported, tagged with a standardized accounting concept from the
  **US-GAAP XBRL taxonomy** -- e.g. `Assets`, `NetIncomeLoss`,
  `Revenues`. That's what makes this work identically for any ticker.
- `sec_edgar.STATEMENTS` maps each statement's line items to their
  US-GAAP tag(s) (with fallback tags, since not every company tags
  things identically) and builds a table of the most recent quarters.
- Responses are cached on disk in `.cache/` for 12 hours, both to keep
  the app snappy and to be a polite, low-volume user of a free public
  service.

## Extending it

Adding a new line item to an existing statement, or a whole new one, is
just editing the `STATEMENTS` dictionary near the top of `sec_edgar.py`
-- no other code changes needed. For example, to add "Inventory" to the
balance sheet:

```python
LineItem("Inventory", ["InventoryNet"], "instant"),
```

Good next steps once the basics feel comfortable: annual (10-K) vs.
quarterly (10-Q) toggle, comparing two companies side by side, or a
simple line chart of a metric over time (Streamlit's `st.line_chart`
takes a DataFrame directly).
