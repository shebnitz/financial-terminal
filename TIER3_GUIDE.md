# Tier 3 Guide: Comparables Analysis

This is the comp table from your screenshot -- Price / Market Cap / EV
across the top, Sales / EBITDA / EBIT / Earnings / Beta in the middle,
EV/Sales / EV/EBITDA / EV/EBIT / P/E on the right, with Average and
Median rows at the bottom -- for up to 10 tickers at once, on its own
tab next to Single Company.

Five files changed: `sec_edgar.py` (the new data layer), `app.py` (the
new tab), `requirements.txt` (one new package), and two new test files,
`tests/test_comparables.py` and `tests/test_app_comparables.py`. The
project charter's been updated too -- bumped to v1.1, with Section 5
(now titled "Comparables Analysis") and the Tier 3 checklist both
reflecting what actually shipped.

**Since-shipped tweaks (v1.2):** the "TEV" column is now labeled "EV"
(same figure -- Enterprise Value, Market Cap + Total debt - Cash --
just the shorter name Kevin uses on his own sheets), and a Beta column
was added between Earnings and EV/Sales, with an Average row (a plain
arithmetic mean) but deliberately no Median -- Kevin only asked for the
average. A Finviz-parsing bug fix landed in between too: see
`sec_edgar.py`'s `_safe_finviz_number_convert` for why a blank field on
Finviz's page used to crash the whole table instead of just leaving
that cell empty.

---

## Part 1 -- See it, run it, try it

### 1. Install the one new dependency

This feature needs a new package -- `requirements.txt` now lists
`finvizfinance`, but your `.venv` doesn't have it installed yet:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**You'll know it worked when** pip reports installing `finvizfinance`
(and its own dependencies) with no errors.

### 2. Look at the diffs in VS Code

Source Control panel, same as always. `sec_edgar.py` has a new section
near the bottom ("Comparables Analysis (Tier 3)"); `app.py`'s diff wraps
the whole existing page in `st.tabs(...)` and adds a second tab's worth
of new UI.

### 3. Run the tests

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**You'll know it worked when** you see `51 passed` (up from 36 -- 11 new
tests in `tests/test_comparables.py` for the math, 4 more in
`tests/test_app_comparables.py` for the actual tab).

### 4. Run the app and try it

```powershell
.venv\Scripts\python.exe -m streamlit run app.py
```

Click the **Comparables Analysis** tab. It starts pre-filled with `KO,
PEP, KDP, MNST, FIZZ` -- the same five beverage companies from your
screenshot -- so you can compare the output directly against it. Click
**Run Comparables Analysis** and watch the progress bar; each ticker
means several SEC EDGAR calls plus one Finviz lookup, so ten tickers can
take a little while. Try typing a bad ticker into the list (e.g. `KO,
NOTATICKER`) and running it again -- you should get a table for KO plus
a yellow warning for the bad one, not a crash. Stop the app with
`Ctrl+C` when you're done.

**One thing worth knowing going in:** Finviz numbers won't match your
screenshot exactly, even for the same five companies -- that screenshot
is a snapshot from whenever it was made, and stock prices (so market
caps, so EVs, so every multiple) move every trading day. What you're
checking is the *shape* of the table and that the math holds together
internally, not that the numbers match to the penny.

---

## Part 2 -- What the new syntax means

Open `sec_edgar.py` in VS Code, scrolled to the Comparables Analysis
section, for this part.

### Finviz via `finvizfinance`, and why it's a different kind of dependency

```python
from finvizfinance.quote import finvizfinance as _FinvizQuote
...
quote = _FinvizQuote(ticker)
fundament = quote.ticker_fundament(raw=False)
```

Every other data source in this app -- SEC EDGAR -- is a real,
documented, stable API: SEC publishes exactly what URLs return what
JSON, and that contract doesn't change without notice. Finviz has no
such thing. `finvizfinance` works by downloading finviz.com's own quote
page and picking numbers out of its HTML -- which is the only free way
to get live stock prices without signing up for a paid data feed, but it
means Finviz redesigning that page could break this overnight with no
warning, where SEC EDGAR never has. `raw=False` is what turns Finviz's
formatted page text ("168.04B") into a plain float (168040000000.0) for
us -- worth knowing that conversion exists, even though you'll never
call it directly.

### Why `get_market_data()` catches `Exception`, not just specific errors

```python
except Exception as e:  # noqa: BLE001 -- deliberately broad, see docstring above
    raise MarketDataError(f"Couldn't get Finviz market data for '{ticker}': {e}") from e
```

Catching bare `Exception` is usually a code smell -- it can hide bugs by
swallowing errors you didn't mean to catch. This is one of the rare
places it's the right call: `finvizfinance` can fail in ways specific to
*how* it's scraping (a changed page layout, a network hiccup, a
different error type finviz.com's server returns on a bad day), and none
of those are things this app can do anything different about. The goal
is simple: whatever went wrong reaching Finviz becomes one
`MarketDataError` with a readable message, so `build_comparables_table()`
can catch exactly that one type and keep going with the rest of the
tickers. The `# noqa: BLE001` comment tells a linter "yes, this bare
`except` is intentional" -- BLE001 is the specific lint rule name for
"blind except," in case you ever see it flagged elsewhere and wonder
what it means.

### TTM: `_ttm_sum()`

```python
def _ttm_sum(df: pd.DataFrame, label: str) -> float | None:
    if label not in df.index:
        return None
    values = df.loc[label].dropna()
    if values.empty:
        return None
    return float(values.sum())
```

`df.loc[label]` pulls out one row (e.g. "Total revenue") as a pandas
`Series` across whatever columns (period ends) `df` has -- since
`build_comparables_table()` always calls `build_statement(...,
n_periods=4)` first, that's up to the last four quarters.
`.dropna()` drops any blank quarters before summing, so a company
missing one quarter's data still gets a TTM figure from the other
three (understated, but not crashed) rather than the whole sum coming
back as `NaN` the moment any single quarter is missing.

### Star-marking: `_company_period_is_annual_only()`

```python
latest_end = max(points, key=lambda end: pd.Timestamp(end))
latest_point = points[latest_end]
return bool(latest_point.get("derived")) or latest_point.get("form") == "10-K"
```

This reuses `_quarterly_points()` -- the same function behind the
cumulative-YTD-derivation fix from the last round of bug fixes -- one
level closer to the raw data than `build_statement()` normally exposes,
specifically to answer one question: is this company's most recent
period actually a single quarter, or did it have to come from a whole
fiscal year (either because SEC's own `form` field says `"10-K"`, or
because `_quarterly_points()` had to derive it from a year-to-date
cumulative total)? Either way, `build_comparables_table()` appends
`" *"` to that company's name in the table, matching Charter Section 5's
star-marking rule.

### The two-exception-types pattern

```python
class SecEdgarError(Exception):
    ...

class MarketDataError(Exception):
    ...
```

Two small, separate exception classes instead of one shared one. The
payoff shows up in `build_comparables_table()`:

```python
except (SecEdgarError, MarketDataError) as e:
    errors[ticker] = str(e)
```

Both get caught the same way here, but keeping them separate means any
*other* code that only cares about one source's failures (say, a future
feature that only touches SEC data) can catch just `SecEdgarError`
without also swallowing an unrelated Finviz problem it wasn't expecting.

### Per-ticker error isolation, and `dict.setdefault`-free row building

```python
rows: dict[str, dict[str, float | None]] = {}
errors: dict[str, str] = {}

for ticker in tickers:
    try:
        ...
        rows[name] = {...}
    except (SecEdgarError, MarketDataError) as e:
        errors[ticker] = str(e)
```

One `try`/`except` wraps the ENTIRE per-ticker pipeline -- CIK lookup,
three `build_statement()` calls, the Finviz lookup, every calculation --
so a failure at any single step for one ticker skips only that ticker's
row and keeps going with the rest of the list. `build_comparables_table()`
returns `(dataframe, errors)` as a tuple rather than raising on the
first bad ticker, which is what lets `app.py` show nine good rows and
one yellow warning instead of one big red error and nothing else.

### Average/Median: built with a plain dict, not a pandas groupby

```python
summary: dict[str, dict[str, float | None]] = {"Average": {}, "Median": {}}
for col in _COMPARABLES_MULTIPLE_COLUMNS:
    values = df[col].dropna()
    summary["Average"][col] = float(values.mean()) if not values.empty else None
    summary["Median"][col] = float(values.median()) if not values.empty else None
for label in ("Average", "Median"):
    df.loc[label] = {col: summary[label].get(col) for col in COMPARABLES_COLUMNS}
```

`df.loc[label] = {...}` is a neat pandas trick worth remembering: assign
a *dict* to a new row label, and pandas lines each dict key up with the
matching column automatically -- any column left out of the dict (every
non-multiple column here) becomes `NaN`, which is exactly the blank cell
the real comp table shows on its Average/Median rows.

### `app.py`: tabs wrap almost the whole page

```python
tab_single, tab_comparables = st.tabs(["Single Company", "Comparables Analysis"])

with tab_single:
    ...  # everything the app already did
with tab_comparables:
    ...  # the new stuff
```

`st.tabs()` returns one object per tab name, and `with tab_x:` scopes
everything indented under it to show up only on that tab. The Single
Company tab's contents didn't change at all in this diff -- the whole
block just got re-indented one level to sit inside `with tab_single:`.
The sidebar stays outside both tabs (it's declared before `st.tabs()`
runs), so it's visible no matter which tab you're on -- for now that's
fine, since its controls (ticker, statement, presets) are all
Single-Company-specific and simply don't affect the Comparables
Analysis tab either way.

---

## Part 3 -- What's still open (from the charter, being upfront)

- **Quarterly / TTM toggle** -- Comparables Analysis is TTM-only right
  now; there's no way to switch it to a single most-recent-quarter view,
  and Single Company still has no period-mode control at all. Charter
  Tier 3 checklist item 6, still unchecked.
- **The original `build_peer_comparison()` function never got built** --
  your screenshot gave a concrete, better target to build toward, so
  `build_comparables_table()` shipped in its place. The charter's
  Section 5 now has a note explaining the swap.
- **Finviz reliability** -- see Part 2's note on why this is a
  fundamentally less stable dependency than SEC EDGAR. If Comparables
  Analysis suddenly starts failing on every ticker someday, a Finviz
  page redesign breaking `finvizfinance` is the first thing to suspect.

---

## Part 4 -- Ship it: branch, commit, push, pull request, merge

```powershell
git checkout -b feature/comparables-analysis
git add sec_edgar.py app.py requirements.txt tests/test_comparables.py tests/test_app_comparables.py TIER3_GUIDE.md
git commit -m "Add Comparables Analysis tab (Tier 3): EV/EBITDA/EBIT, Beta, and valuation multiples across up to 10 tickers"
git push -u origin feature/comparables-analysis
```

Click the URL the output gives you, **Compare & pull request**, skim the
diff, **Create pull request**, then **Merge pull request** ->
**Confirm merge**. Back in VS Code:

```powershell
git checkout main
git pull
git branch -d feature/comparables-analysis
```

**You'll know it worked when** `git status` says you're on `main` with
nothing to commit, `pytest tests/ -v` shows `51 passed`, and the
Comparables Analysis tab works when you run the app.

The project charter (v1.1) is up to date on claude.ai -- nothing to pull
for that one, it's not a file in your repo.
