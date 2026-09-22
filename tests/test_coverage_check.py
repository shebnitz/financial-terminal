"""
Tests for coverage_check.py -- the tag-gap diagnostic tool. These use the
same monkeypatch-fake-facts pattern as test_sec_edgar.py, so no real
network access is needed to run them.

Run with:  python -m pytest tests/test_coverage_check.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coverage_check  # noqa: E402
import sec_edgar  # noqa: E402


def _instant(end: str, val: float, form: str = "10-Q", filed: str = "2026-08-01") -> dict:
    return {"end": end, "val": val, "form": form, "filed": filed}


def _duration(start: str, end: str, val: float, form: str = "10-Q", filed: str = "2026-08-01") -> dict:
    return {"start": start, "end": end, "val": val, "form": form, "filed": filed}


def test_camel_words_splits_pascal_case_tag_names():
    assert coverage_check._camel_words("PropertyPlantAndEquipmentNet") == {
        "property", "plant", "and", "equipment", "net",
    }


def test_find_candidate_tags_ranks_by_keyword_overlap_and_excludes_known_tags():
    all_tags = [
        "PropertyPlantAndEquipmentNet",  # already a known tag -- excluded
        "PropertyPlantAndEquipmentGross",  # shares "property"/"plant"/"equipment" -- 3-way overlap
        "SomeUnrelatedTag",  # no overlap
        "EquipmentLeaseExpense",  # shares only "equipment" -- 1-way overlap
    ]
    keywords = {"property", "plant", "equipment"}
    candidates = coverage_check._find_candidate_tags(
        all_tags, keywords, exclude={"PropertyPlantAndEquipmentNet"}
    )
    names = [tag for tag, score in candidates]
    assert "PropertyPlantAndEquipmentNet" not in names  # excluded even though it matches
    assert "SomeUnrelatedTag" not in names  # no keyword overlap at all
    assert names[0] == "PropertyPlantAndEquipmentGross"  # best match ranked first


def test_check_company_reports_a_gap_and_a_useful_candidate(monkeypatch, capsys):
    # A company that tags PP&E under a completely different (but
    # keyword-similar) name than either of our two candidate tags --
    # simulating the exact kind of drift that motivated this tool.
    facts = {
        "entityName": "Renamed Tag Corp",
        "facts": {
            "us-gaap": {
                # Another Balance Sheet field DOES have data at this
                # period end, so the tool knows 2026-06-30 is a period
                # this company reports for at all.
                "Assets": {"units": {"USD": [_instant("2026-06-30", 500000)]}},
                # Neither of the tags Property/equipment currently looks
                # for is present -- both candidate tags come up empty,
                # so this should show up as a GAP.
                "PropertyPlantAndEquipmentGrossBeforeSomeAdjustment": {
                    "units": {"USD": [_instant("2026-06-30", 99000)]}
                },
            }
        },
    }
    monkeypatch.setattr(sec_edgar, "get_cik_for_ticker", lambda ticker: "0000000000")
    monkeypatch.setattr(sec_edgar, "company_name", lambda cik: facts["entityName"])
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: facts)

    found_gap = coverage_check.check_company("RENAMED", n_periods=1)
    out = capsys.readouterr().out

    assert found_gap is True
    assert "GAP  Property and equipment, net" in out
    assert "PropertyPlantAndEquipmentGrossBeforeSomeAdjustment" in out  # suggested as a candidate


def test_check_company_reports_no_gaps_when_a_field_is_fully_covered(monkeypatch, capsys):
    facts = {
        "entityName": "Fully Covered Corp",
        "facts": {
            "us-gaap": {
                "Assets": {"units": {"USD": [_instant("2026-06-30", 1000)]}},
            }
        },
    }
    # Shrink STATEMENTS down to just one field for this test so the
    # output is easy to reason about, without needing every field this
    # company doesn't report to also be faked out.
    monkeypatch.setattr(
        sec_edgar,
        "STATEMENTS",
        {"Balance Sheet": [sec_edgar.LineItem("Total assets", ["Assets"], "instant", category="Assets")]},
    )
    monkeypatch.setattr(sec_edgar, "get_cik_for_ticker", lambda ticker: "0000000000")
    monkeypatch.setattr(sec_edgar, "company_name", lambda cik: facts["entityName"])
    monkeypatch.setattr(sec_edgar, "get_company_facts", lambda cik: facts)

    found_gap = coverage_check.check_company("COVERED", n_periods=1)
    out = capsys.readouterr().out

    assert found_gap is False
    assert "No gaps" in out


def test_check_company_handles_a_bad_ticker_without_crashing(monkeypatch, capsys):
    def _raise(ticker):
        raise sec_edgar.SecEdgarError("Couldn't find ticker 'NOPE' in SEC's company list.")

    monkeypatch.setattr(sec_edgar, "get_cik_for_ticker", _raise)

    found_gap = coverage_check.check_company("NOPE", n_periods=1)
    out = capsys.readouterr().out

    assert found_gap is False
    assert "couldn't fetch data" in out


if __name__ == "__main__":
    print("Running coverage_check.py tests manually (prefer `pytest tests/` normally)...")
    test_camel_words_splits_pascal_case_tag_names()
    print("  test_camel_words_splits_pascal_case_tag_names: OK")
    test_find_candidate_tags_ranks_by_keyword_overlap_and_excludes_known_tags()
    print("  test_find_candidate_tags_ranks_by_keyword_overlap_and_excludes_known_tags: OK")
    print("All manual checks passed (run pytest for the full suite, including capsys-based tests).")
