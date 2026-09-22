"""
coverage_check.py
==================
Answers your "point 3" concern directly: since different companies tag
the same real-world line item under different XBRL names -- and even
the SAME company can rename a tag mid-history (this project has already
hit that three separate times: Revenue, PP&E, Accounts payable) -- how
do we find these gaps BEFORE they show up as a blank cell in the app,
instead of after?

This script is the proactive half of the answer. It does NOT try to
guess every possible tag name in advance (that list would be enormous
and still incomplete -- US-GAAP has thousands of tags). Instead, for
any ticker you give it, it:

  1. Checks every field STATEMENTS defines against that company's own
     real filing data, and flags any recent quarter where none of our
     candidate tags found a value ("a gap").
  2. For each gap, searches EVERY OTHER us-gaap tag that company has
     ever used, looking for ones whose name shares meaningful words
     with our existing candidate tags (e.g. a gap in "Property and
     equipment, net" -- built from tags containing "property",
     "plant", "equipment" -- would surface some other tag this company
     uses that also contains those words), and reports which of those
     actually have data for the missing quarter(s).

That second part is the "comprehensive if-then fallback list" you asked
about, but built the honest way: rather than a giant hand-written list
of guesses (most of which would be wrong for any given company, and
none of which would help once SEC or a company invents a brand new tag
next year), this tool regenerates candidate suggestions fresh, from
whatever tags that specific company is actually using RIGHT NOW. It
turns "quarterly archaeology in a sandbox with restricted internet
access" into a report you can run yourself, any time, in seconds, on
your own machine's unrestricted connection.

This does NOT edit sec_edgar.py automatically -- it only suggests. The
same verify-before-you-trust-a-tag habit this project has used every
time so far (checking a candidate tag's numbers actually match a real
filing) still applies: read the suggested tag's numbers, sanity-check
them against the company's actual 10-Q/10-K, and only then add it to
that LineItem's `tags` list in sec_edgar.py.

Usage (run this on YOUR machine, not the sandbox -- see the note at the
bottom of this file about why):

    python coverage_check.py META
    python coverage_check.py META AAPL MSFT TSLA     # check several at once
    python coverage_check.py META --periods 8        # look back further than 4 quarters
"""

from __future__ import annotations

import argparse
import re

import sec_edgar

# Generic accounting/XBRL words that show up in tons of unrelated tag
# names (e.g. almost every balance-sheet tag ends in "Net" or
# "Current") -- stripping these out before comparing tag names to each
# other is what keeps the candidate search from drowning in noise.
_STOPWORDS = {
    "and", "of", "the", "for", "in", "on", "net", "gross", "total",
    "other", "current", "noncurrent", "value", "carrying", "extraordinary",
}


def _camel_words(tag: str) -> set[str]:
    """Split a PascalCase XBRL tag name into lowercase words -- XBRL tags
    don't have spaces, so 'PropertyPlantAndEquipmentNet' needs to become
    {'property', 'plant', 'and', 'equipment', 'net'} before we can compare
    it word-by-word against another tag name. `re.findall` with this
    pattern grabs each run that starts with a capital letter (one "word"
    of the camelCase) or, as a fallback, any lowercase/digit run."""
    words = re.findall(r"[A-Z][a-z0-9]*|[a-z0-9]+", tag)
    return {w.lower() for w in words}


def _keywords_for_item(item: sec_edgar.LineItem) -> set[str]:
    """The meaningful words a line item is "about", built from its OWN
    candidate tags -- not its plain-English label. Labels use common
    English words ("net", "other") that are too generic to search tag
    names with; tags themselves are more specific and more consistent
    with how the rest of the taxonomy is named."""
    words: set[str] = set()
    for tag in item.tags:
        words |= _camel_words(tag)
    return words - _STOPWORDS


def _find_candidate_tags(all_tags: list[str], keywords: set[str], exclude: set[str]) -> list[tuple[str, int]]:
    """Every us-gaap tag this company has ever reported under (excluding
    ones we already try) that shares at least 2 meaningful words with
    `keywords` (or 1, if a field's own tags only ever offered 1 to begin
    with), sorted by how many words it shares (most first). Returns
    (tag_name, overlap_score) pairs.

    Fixed at "2" rather than "half of however many keywords this field
    has" on purpose: a field whose own candidate tags happen to be long
    (e.g. Property and equipment's second tag folds in finance-lease
    wording, contributing a dozen keywords) would otherwise need an
    unreasonably high overlap to match anything, drowning out a
    perfectly good short candidate tag that only shares the 2-3 words
    that actually matter ("property", "plant", "equipment")."""
    if not keywords:
        return []
    threshold = 1 if len(keywords) <= 2 else 2
    scored = []
    for tag in all_tags:
        if tag in exclude:
            continue
        overlap = len(_camel_words(tag) - _STOPWORDS & keywords)
        if overlap >= threshold:
            scored.append((tag, overlap))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def check_company(ticker: str, n_periods: int = 4) -> bool:
    """Print a coverage report for one ticker. Returns True if at least
    one gap was found (useful if you ever want to script this, e.g.
    "exit non-zero if any of my watchlist has a new gap")."""
    try:
        cik = sec_edgar.get_cik_for_ticker(ticker)
        name = sec_edgar.company_name(cik)
        facts = sec_edgar.get_company_facts(cik)
    except sec_edgar.SecEdgarError as e:
        print(f"\n{ticker}: couldn't fetch data -- {e}")
        return False

    all_tags = list(facts.get("facts", {}).get("us-gaap", {}).keys())

    print(f"\n{'=' * 70}")
    print(f"{ticker} -- {name} (CIK {cik})")
    print(f"{'=' * 70}")

    found_any_gap = False

    for statement, items in sec_edgar.STATEMENTS.items():
        # Recompute, per item, exactly which period ends OUR current
        # candidate tags find data for -- the same merge-across-tags
        # logic build_statement() itself uses, so "missing" here means
        # exactly what would show up blank in the app.
        item_points: dict[str, dict] = {}
        all_period_ends: set[str] = set()
        for item in items:
            points: dict[str, dict] = {}
            for tag in item.tags:
                entries = sec_edgar._facts_for_tag(facts, tag, unit=item.unit)
                for end, point in sec_edgar._quarterly_points(entries, item.kind).items():
                    points.setdefault(end, point)
            item_points[item.label] = points
            all_period_ends.update(points.keys())

        period_ends = sorted(all_period_ends, reverse=True)[:n_periods]
        if not period_ends:
            # Nothing in this statement has ANY data for this company at
            # all -- unusual, but not this tool's job to explain.
            continue

        statement_gaps = []
        for item in items:
            points = item_points[item.label]
            missing = [end for end in period_ends if end not in points]
            if not missing:
                continue
            statement_gaps.append((item, missing))

        if not statement_gaps:
            continue

        found_any_gap = True
        print(f"\n-- {statement} (checked {', '.join(period_ends)}) --")
        for item, missing in statement_gaps:
            covered = len(period_ends) - len(missing)
            note = "  [has a fallback formula, so the app may still show a computed value]" if item.fallback else ""
            print(f"  GAP  {item.label}: {covered}/{len(period_ends)} periods covered{note}")

            keywords = _keywords_for_item(item)
            candidates = _find_candidate_tags(all_tags, keywords, exclude=set(item.tags))

            # Only worth mentioning a candidate if it actually has data
            # for one of the missing periods -- otherwise it wouldn't
            # close the gap even if the name looks related.
            useful = []
            for tag, score in candidates:
                entries = sec_edgar._facts_for_tag(facts, tag, unit=item.unit)
                tag_points = sec_edgar._quarterly_points(entries, item.kind)
                hits = [end for end in missing if end in tag_points]
                if hits:
                    useful.append((tag, score, hits))

            for tag, score, hits in useful[:5]:
                print(f"         try: {tag}   (covers {len(hits)}/{len(missing)} missing period(s), e.g. {hits[0]})")
            if not useful:
                print("         (no similarly-named tag in this company's own filings covers the gap --"
                      " it likely just isn't broken out separately)")

    if not found_any_gap:
        print("\n  No gaps -- every field is fully covered for the periods checked.")

    return found_any_gap


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan one or more tickers for fields our STATEMENTS "
        "definitions can't find data for, and suggest candidate XBRL "
        "tags (from that company's own filings) that might close the gap."
    )
    parser.add_argument("tickers", nargs="+", help="One or more tickers, e.g. META AAPL TSLA")
    parser.add_argument(
        "--periods", type=int, default=4, help="How many recent quarters to check (default: 4)"
    )
    args = parser.parse_args()

    any_gap = False
    for ticker in args.tickers:
        gap = check_company(ticker.strip().upper(), n_periods=args.periods)
        any_gap = any_gap or gap

    print()
    if not any_gap:
        print("All checked tickers are fully covered for the periods checked.")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# Why run this on YOUR machine, not ask Claude to run it in the sandbox:
# ---------------------------------------------------------------------------
# The cloud sandbox this app gets built in can't reach data.sec.gov
# directly (its outbound network is restricted to an allowlist that
# doesn't include SEC's API) -- every tag verified so far this session
# was checked indirectly, through a web-fetch tool, one small request at
# a time. Your machine has no such restriction: `python coverage_check.py`
# talks to data.sec.gov exactly the same way `streamlit run app.py`
# already does. Running this yourself, whenever you add a new ticker to
# your regular workflow or notice a blank cell, is faster and more
# reliable than asking for another round of sandbox archaeology.
