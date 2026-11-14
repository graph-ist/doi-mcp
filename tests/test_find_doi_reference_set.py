"""find_doi against an offline reference set: precision and recall are asserted, so a change to
the matching rules shows up as a number, not as a surprise in use."""
import asyncio
import json
import os

import pytest

from paper_doi_mcp import server as s

CASES = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "find_doi_cases.json"),
                       encoding="utf-8"))["cases"]
MIN_PRECISION = 1.0   # a wrong DOI is the failure the tool exists to prevent
MIN_RECALL = 0.85     # known gaps are counted; raise this when a gap is closed


def _run(case):
    src = case["sources"]

    def canned(name):
        async def fn(c, *a, **k):
            if src.get(name) == "error":
                raise RuntimeError(f"{name} down")
            return [s._cand(name, **{k2: v for k2, v in r.items()}) for r in src.get(name, [])]
        return fn

    async def none_list(c, *a, **k):
        return []

    async def ctype(doi):
        return case.get("book_types", {}).get(doi)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(s, "_http", lambda: None)
        mp.setattr(s, "_crossref", canned("crossref"))
        mp.setattr(s, "_semantic_scholar", canned("semantic_scholar"))
        mp.setattr(s, "_openalex", canned("openalex"))
        mp.setattr(s, "_open_library", none_list)
        mp.setattr(s, "_book_google_books", none_list)
        mp.setattr(s, "_crossref_type", ctype)
        return asyncio.run(s.find_doi(**case["cited"]))


def _outcome(case, out):
    exp = case["expected"]
    got_doi = out["best"]["doi"] if out["best"] else None
    if exp["status"] == "match":
        return "tp" if got_doi == exp["doi"] else ("fp" if got_doi else "fn")
    return "fp" if got_doi else "tn"


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_reference_case(case):
    out = _run(case)
    exp = case["expected"]
    if case.get("known_gap"):
        assert out["best"] is None or out["best"]["doi"] == exp["doi"]
        pytest.xfail(case["known_gap"])
    assert out["status"] == exp["status"], out
    if exp["status"] == "match":
        assert out["best"]["doi"] == exp["doi"]
    if "competing" in exp:
        assert out["agreement"]["competing_dois"] == exp["competing"]
    if "independent_sources" in exp:
        assert out["agreement"]["independent_sources"] == exp["independent_sources"]
    if "sources" in exp:
        assert len(out["best"]["sources"]) == exp["sources"]
    if "excluded_derivatives" in exp:
        assert out["excluded_derivatives"] == exp["excluded_derivatives"]
    if "source_errors" in exp:
        assert len(out["source_errors"]) == exp["source_errors"]
    if "note_contains" in exp:
        assert exp["note_contains"] in out.get("note", "")


def test_precision_and_recall_over_the_reference_set():
    counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for case in CASES:
        counts[_outcome(case, _run(case))] += 1
    precision = counts["tp"] / (counts["tp"] + counts["fp"])
    recall = counts["tp"] / (counts["tp"] + counts["fn"])
    print(f"\nfind_doi reference set: {counts}, precision {precision:.2f}, recall {recall:.2f}")
    assert precision >= MIN_PRECISION, counts
    assert recall >= MIN_RECALL, counts
