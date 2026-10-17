import server as s


def cand(title, authors=("Smith",), year=2010, **kw):
    return {"title": title, "authors": list(authors), "year": year, **kw}


def eligible(c, title, author=None, year=None):
    return s._eligible(s._assess(c, title, author, year))


def test_non_latin_titles_do_not_collapse():
    assert s._norm("Бытие и время") == "бытие и время"
    assert s._title_sim("Бытие и время", "Война и мир")[0] < s.TITLE_THRESHOLD


def test_accents_are_folded():
    assert s._norm("Phénoménologie de la perception") == "phenomenologie de la perception"


def test_generic_title_wrong_author_is_rejected():
    assert not eligible(cand("Introduction"), "Introduction", "Norman", 2010)


def test_short_title_needs_author():
    assert not eligible(cand("Introduction"), "Introduction")
    assert eligible(cand("Introduction", ["Donald A. Norman"]), "Introduction", "Norman")


def test_author_surname_is_token_not_substring():
    assert s._author_ok("Li", ["Williams"]) is False
    assert s._author_ok("Fox, M. C.", ["Mark C. Fox"]) is True
    assert s._author_ok("van der Berg", ["J. van der Berg"]) is True
    assert s._author_ok("Norman", []) is None


def test_year_outside_tolerance_is_rejected():
    t = "The ecological approach to visual perception"
    assert not eligible(cand(t, ["Gibson"], 1950), t, "Gibson", 1979)


def test_derivative_records_are_detected():
    t = "The ecological approach to visual perception in interaction design"
    assert s._is_derivative(cand("Correction to: " + t), t)
    assert s._is_derivative(cand("Erratum to \u201c" + t + "\u201d"), t)
    assert s._is_derivative(cand(t, type="peer-review"), t)
    assert not s._is_derivative(cand("Response inhibition in older adults"), "x")
    assert not s._is_derivative(cand("Reply to: " + t), "Reply to: " + t)


def test_subtitle_variants():
    full = "Distributed cognition: Toward a new foundation for human-computer interaction research"
    assert s._title_sim("Distributed cognition", full)[0] == 1.0
    assert s._title_sim(full, "Distributed cognition",
                        "Toward a new foundation for human-computer interaction research")[0] > 0.95
    # Head-only matches are short, so they still need the author
    assert not eligible(cand(full, ["Hollan"]), "Distributed cognition")
    assert eligible(cand(full, ["Hollan"]), "Distributed cognition", "Hollan")


def test_html_and_retracted_prefix_are_ignored():
    assert s._title_sim("Drosophila wing development", "<i>Drosophila</i> wing development")[0] == 1.0
    assert s._title_sim("Ileal lymphoid nodular hyperplasia",
                        "RETRACTED: Ileal-lymphoid-nodular hyperplasia")[0] == 1.0


def test_clean_doi():
    assert s._clean_doi("https://doi.org/10.1000/abc?x=1#y") == "10.1000/abc"
    assert s._clean_doi("https://doi.org/10.1000/abc%2Fdef") == "10.1000/abc/def"
    assert s._clean_doi("(see doi:10.1000/xyz).") == "10.1000/xyz"
    assert s._clean_doi("10.1016/S0140-6736(97)11096-0") == "10.1016/s0140-6736(97)11096-0"
    assert s._clean_doi("10.1000/foo_(bar)") == "10.1000/foo_(bar)"
    assert s._clean_doi("not a doi") is None


def test_doi_path_escapes_query_characters():
    assert s._doi_path("10.1000/a?b#c") == "10.1000/a%3Fb%23c"


def test_page_ranges():
    assert s._biblio_norm("123\u201345") == s._biblio_norm("123-145")
    assert s._biblio_norm("e12345") == "e12345"


def test_error_message_never_empty():
    import httpx
    assert s._err("crossref", httpx.ReadTimeout("")) == "crossref: ReadTimeout"


def test_graph_links():
    nodes = {
        "S": {"id": "S", "refs": ["A", "B", "X"]},
        "C": {"id": "C", "refs": ["A", "B", "S", "X"]},
        "A": {"id": "A", "refs": []},
        "B": {"id": "B", "refs": ["Y"]},
    }
    edges, cocit, coupling = s._graph_links(nodes, ["S"], min_cocitation=2, min_coupling=2)
    assert ["C", "S"] in edges and ["S", "A"] in edges
    assert ["A", "B", 2] in cocit
    # S and C share A, B and X in their full reference lists
    assert coupling[0][:3] == ["C", "S", 3] and 0 < coupling[0][3] <= 1
    assert nodes["C"]["seeds_linked"] == 1 and nodes["S"]["seeds_linked"] == 0
