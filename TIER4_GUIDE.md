# Tier 4 Guide: Polish & Robustness

Two things, both from Charter Section 7's Tier 4 checklist: an **Annual
(10-K) vs. Quarterly (10-Q)** choice for the Single Company tab, and
**clearer error messages** for the network problems that were always
possible but never handled well -- SEC EDGAR being briefly down, or SEC
rate-limiting this app. The checklist's third item ("handle the
fiscal-period-alignment decision from Section 9") turned out to already
be done -- Tier 3's MRQ-alignment-plus-star-marking work *was* that
decision being handled, so there's nothing new to build for it here;
the charter just marks it done and points back to Tier 3.

Three files changed: `sec_edgar.py` (the new `period_type` param and
the retry logic), `app.py` (the new sidebar radio), and three new test
files.

---

## Part 1 -- See it, run it, try it

### 1. Look at the diffs

Source Control panel, same as always. `sec_edgar.py` has two separate
changes worth telling apart: `_get_json()` grew a retry loop (nothing
about what it returns changed, just how hard it tries and what it says
when it fails), and `_quarterly_points()` / `build_statement()` grew a
new `period_type` parameter (this one DOES change what comes back).

### 2. Run the tests

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**You'll know it worked when** you see `69 passed` (up from 53 -- 7 new
tests in `tests/test_period_type.py`, 6 in `tests/test_error_handling.py`,
3 more added to `tests/test_app.py`).

### 3. Run the app and try Annual mode

```powershell
.venv\Scripts\python.exe -m streamlit run app.py
```

On the **Single Company** tab, look for the new **Period type** radio
right under the Statement dropdown -- **Quarterly (10-Q)** is picked by
default (nothing about your existing workflow changes unless you touch
this). Switch it to **Annual (10-K)**, notice the slider below it
relabels itself to "How many recent fiscal years?", and click **Fetch
data**. You should get META's last few *fiscal years* instead of
quarters -- the subheader will say "(Annual, 10-K)" so it's obvious at
a glance which mode produced what's on screen. Try Balance Sheet too --
Annual mode should show fiscal year-end balances, not every quarter-end
balance the way Quarterly mode does.

**One edge case worth trying on purpose:** a very recently IPO'd
company (one or two 10-Ks filed, ever) in Annual mode with a high
"fiscal years" count -- you should get a plain warning ("No annual
(10-K) data found...") rather than a blank, confusing table.

Stop the app with `Ctrl+C` when you're done.

---

## Part 2 -- What the new syntax means

### `period_type`: one parameter, two completely different filters

Open `sec_edgar.py`'s `_quarterly_points()`. The whole function used to
assume "quarterly" -- everywhere it filtered by day-count (`75 <= days
<= 100`) or by form (`("10-Q", "10-K")`), those were hardcoded. Adding
Annual mode meant turning both into a choice:

```python
annual = period_type == "annual"
allowed_forms = ("10-K",) if annual else ("10-Q", "10-K")
```

`allowed_forms` matters more than it looks. Annual figures are what a
10-K itself reports; a 10-Q basically never carries a full fiscal
year's duration. Restricting to `("10-K",)` in annual mode is what
makes Annual mode actually show *fiscal years* and not just "whatever
quarterly data happens to span close to 365 days" (which shouldn't
exist, but defensive code doesn't assume the data will always behave).

The day-window check moved the same way:

```python
if annual:
    if not (350 <= days <= 380):
        continue
else:
    ...  # the original 75 <= days <= 100 quarterly check, unchanged
```

### Why annual mode skips the cumulative-derivation pass entirely

The big block in `_quarterly_points()` that derives a missing Q4 from
"full year minus nine months" (from the tag-coverage bug-fixing round)
only makes sense for quarterly mode -- it exists because a discrete
*quarter* sometimes isn't directly reported. A fiscal *year* total, by
contrast, is exactly what a 10-K's own duration entry already is --
nothing to derive. The code reflects that directly:

```python
if kind == "duration" and not annual:
    for start, chain in by_start.items():
        ...
```

Annual mode's fiscal-year points get built entirely in the first pass,
straight from the `days` check above -- the whole derivation block
below it simply doesn't run.

### The Balance Sheet gotcha: `allowed_forms` has to apply to `instant` too

Balance Sheet fields are all `kind="instant"` (a snapshot, not a
span), and the original code let through every instant entry from
*either* a 10-Q or a 10-K. If `allowed_forms` only affected the
`duration` branch, Annual mode would have filtered Income Statement and
Cash Flow Statement correctly but left the Balance Sheet completely
unchanged -- still showing every quarter-end. The fix is that
`allowed_forms` is checked once, at the very top of the loop, before
either branch:

```python
for e in entries:
    if e.get("form") not in allowed_forms:
        continue
    if kind == "duration":
        ...
```

`tests/test_period_type.py`'s
`test_build_statement_annual_mode_also_restricts_instant_balance_sheet_fields`
exists specifically because this is easy to get wrong -- it's exactly
the kind of thing that "looks like it works" in a quick manual test
(Income Statement) but is silently broken for a different statement
(Balance Sheet) that a manual test might not think to try.

### The `_get_json()` retry loop, and why it's a `for` loop with `continue`, not recursion

```python
for attempt in range(_MAX_HTTP_ATTEMPTS):
    is_last_attempt = attempt == _MAX_HTTP_ATTEMPTS - 1
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except requests.exceptions.RequestException as e:
        last_error = str(e)
        if not is_last_attempt:
            time.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
            continue
        raise SecEdgarError(...) from e
    ...
```

A `for` loop with `continue` (try again) or `raise`/`return` (stop) is
a common shape for "retry up to N times" -- it's easier to reason about
than a function calling itself, and there's no risk of Python's
recursion limit getting involved for something that's naturally a
small, bounded loop. `2**attempt` is the classic *exponential* backoff
pattern: attempt 0 waits `2**0 = 1` * base = 2s, attempt 1 waits `2**1
= 2` * base = 4s -- each retry waits longer than the last, on the
theory that a problem which didn't clear up after 2 seconds probably
needs more like 4, not another instant retry.

### Respecting `Retry-After` instead of always using our own backoff

```python
if resp.status_code == 429:
    if not is_last_attempt:
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else _BACKOFF_BASE_SECONDS * (2**attempt)
        time.sleep(delay)
        continue
```

When SEC sends a 429 (rate-limited), it can include a `Retry-After`
header telling you exactly how many seconds to wait -- that's SEC
itself answering the question our own exponential backoff can only
guess at, so it's worth checking for and using when present, falling
back to the normal backoff only when SEC didn't say.

### Why 404 and 403 are never retried, but 429 and 5xx are

`_get_json()` checks `404`/`403` *before* the retry-eligible codes and
handles them with an immediate `raise`, no loop involvement at all.
The distinction is whether trying again could plausibly produce a
different answer: a 404 (bad ticker/CIK) or 403 (rejected User-Agent)
will fail exactly the same way every time -- the *request itself* is
wrong, not the moment you happened to send it. A 429 or 5xx is the
opposite: the request was fine, but the *server* was temporarily unable
or unwilling to answer -- exactly the situation retrying is for.
`tests/test_error_handling.py::test_get_json_404_and_403_are_never_retried`
checks this distinction directly (asserts `requests.get` was called
exactly once).

### `app.py`: a derived string, not two branches of everything

```python
period_type_choice = st.radio("Period type", ["Quarterly (10-Q)", "Annual (10-K)"], horizontal=True)
period_type = "annual" if period_type_choice.startswith("Annual") else "quarterly"
```

The radio's own display labels ("Quarterly (10-Q)", "Annual (10-K)")
are friendlier than the plain `"quarterly"`/`"annual"` strings
`build_statement()` actually wants -- rather than making every
downstream `if` branch on the long display string, one line translates
it once into the short value the data layer's function signature
already expects.

---

## Part 3 -- What's still open

- **Comparables Analysis has no Annual/Quarterly choice of its own** --
  this toggle only touches the Single Company tab. Comparables Analysis
  still always builds TTM figures from each company's last four
  reported quarters; a 10-K-only company there still gets the
  star-marking treatment from Tier 3, unchanged.
- **The Quarterly / TTM toggle (a different thing) is still not
  built** -- don't confuse it with this tier's Annual/Quarterly radio.
  This tier's toggle picks *which filings* to read (10-Q vs 10-K); the
  still-open Quarterly/TTM toggle would pick *how to combine* quarterly
  figures (as-is vs. summed into a trailing twelve months) and was
  never part of Tier 4's own checklist.
- **The retry backoff is fixed, not adaptive** -- 2s, then 4s, always.
  A production system might read SEC's rate-limit headers more
  proactively or track a rolling request budget; this app's caching
  already keeps it well under SEC's ~10 req/sec guidance in normal use,
  so a simple fixed backoff was judged like the right amount of
  robustness for what this app actually needs.

---

## Part 4 -- Ship it: branch, commit, push, pull request, merge

```powershell
git checkout -b feature/polish-and-robustness
git add sec_edgar.py app.py tests/test_period_type.py tests/test_error_handling.py tests/test_app.py TIER4_GUIDE.md
git commit -m "Add Tier 4 polish: Annual (10-K) vs Quarterly (10-Q) toggle, and retry/backoff for SEC network errors"
git push -u origin feature/polish-and-robustness
```

Click the URL the output gives you, **Compare & pull request**, skim the
diff, **Create pull request**, then **Merge pull request** ->
**Confirm merge**. Back in VS Code:

```powershell
git checkout main
git pull
git branch -d feature/polish-and-robustness
```

**You'll know it worked when** `git status` says you're on `main` with
nothing to commit, `pytest tests/ -v` shows `69 passed`, and both the
Period type radio and (if you want to actually see the retry logic
fire) a deliberately-wrong SEC URL still fail with a readable message
instead of a raw traceback.

The project charter is up to date on claude.ai -- nothing to pull for
that one, it's not a file in your repo.
