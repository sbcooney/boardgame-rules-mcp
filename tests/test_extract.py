from bgrules.extract import Line, Section, build_sections, join_lines, normalize_heading
from bgrules.store import query_terms


def line(text, page=1, heading=False, column=0, top=0.0):
    return Line(text=text, page=page, column=column, bbox=(0.0, top, 100.0, top + 10), is_heading=heading)


def test_normalize_small_caps_headings():
    assert normalize_heading("lonGest road") == "Longest Road"
    assert normalize_heading("Build (BuildinG)") == "Build (Building)"
    assert normalize_heading("set-up, variable") == "Set-Up, Variable"
    assert normalize_heading("endinG the Game") == "Ending the Game"


def test_join_lines_dehyphenates_and_drops_icon_space():
    assert join_lines(["The robber begins in the desert .", "It is mov-", "ed only by a 7."]) == (
        "The robber begins in the desert. It is moved only by a 7."
    )
    # A capitalised next line is a real hyphenated term, not a line-break hyphen.
    assert join_lines(["a trade/build-", "Phase"]) == "a trade/build- Phase"


def test_sections_merge_wrapped_headings_and_switch_parts():
    lines = [
        line("intro text"),
        line("game", heading=True),
        line("components", heading=True),
        line("19 hexes"),
        line("build (buildinG)", page=6, heading=True),
        line("You may build.", page=6),
    ]
    parts = [{"name": "Game Rules"}, {"name": "Almanac", "first_heading": "Build (Building)"}]
    secs = build_sections(lines, parts)
    assert [s.path for s in secs] == [
        ["Game Rules", "Introduction"],
        ["Game Rules", "Game Components"],
        ["Almanac", "Build (Building)"],
    ]
    assert secs[2].page_start == 6


def test_passages_split_on_sentence_and_fold_short_tail():
    sec = Section(heading="Long", page_start=1)
    sec.lines = [line("x" * 400 + "."), line("y" * 400 + "."), line("z" * 50 + ".")]
    passages = sec.passages(target_chars=700)
    assert len(passages) == 1  # 50-char tail folded in, no fragment

    sec.lines.append(line("w" * 700 + ".", page=2))
    passages = sec.passages(target_chars=700)
    assert [p.page for p in passages] == [1, 1]


def test_spans_are_per_column():
    sec = Section(heading="Robber", page_start=11)
    sec.lines = [line("a", page=11, column=0, top=400), line("b", page=11, column=1, top=50)]
    assert len(sec.spans) == 2


def test_query_terms_drop_stopwords():
    assert query_terms("Can I build a settlement next to a city?") == ["build", "settlement", "next", "city"]
