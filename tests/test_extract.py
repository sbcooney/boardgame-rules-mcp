from bgrules.extract import Line, Section, _is_full_width, _merge_inline, build_sections, join_lines, normalize_heading
from bgrules.store import expand_terms, query_terms


def line(text, page=1, heading=False, column=0, top=0.0):
    return Line(text=text, page=page, column=column, bbox=(0.0, top, 100.0, top + 10), is_heading=heading)


def test_normalize_small_caps_headings():
    assert normalize_heading("lonGest road") == "Longest Road"
    assert normalize_heading("Build (BuildinG)") == "Build (Building)"
    assert normalize_heading("set-up, variable") == "Set-Up, Variable"
    assert normalize_heading("endinG the Game") == "Ending the Game"
    assert normalize_heading("Now it’s the next player’s turn.") == "Now It’s the Next Player’s Turn."


def test_join_lines_keeps_real_hyphens_and_drops_icon_space():
    # Joined only when the rulebook spells the whole word elsewhere.
    assert join_lines(["The robber begins in the desert .", "It is mov-", "ed only by a 7."], frozenset({"moved"})) == (
        "The robber begins in the desert. It is moved only by a 7."
    )
    assert join_lines(["an award-", "winning rules system"]) == "an award-winning rules system"
    # A spaced dash is punctuation, never a hyphen.
    assert join_lines(["normal in nature -", "such as damage"]) == "normal in nature - such as damage"
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


def test_page_titles_head_sections_and_repeat_as_running_headers():
    def title(text, page):
        ln = line(text, page=page, heading=True)
        ln.is_part = True
        return ln

    lines = [
        title("BIRD POWERS", 6),
        line("food-gaining powers", page=6, heading=True),
        line("Gain 1 food.", page=6),
        title("BIRD POWERS", 8),  # running header: same title atop a later page
        line("More food powers.", page=8),
        line("egg-laying powers", page=8, heading=True),
        line("Lay 1 egg.", page=8),
    ]
    secs = build_sections(lines)
    assert [s.path for s in secs] == [["Bird Powers", "Food-Gaining Powers"], ["Bird Powers", "Egg-Laying Powers"]]
    assert secs[0].text == "Gain 1 food. More food powers."


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


def _raw_line(words: list[tuple[float, float]]) -> dict:
    """A pdfplumber-style line from (x0, x1) character extents."""
    chars = [{"x0": a, "x1": b} for a, b in words]
    return {"x0": chars[0]["x0"], "x1": chars[-1]["x1"], "chars": chars}


def test_full_width_line_vs_two_column_lines_at_same_height():
    mid = 297
    # Continuous text across the middle: word gaps of ~3pt.
    across = _raw_line([(40 + i * 10, 47 + i * 10) for i in range(50)])
    assert _is_full_width(across, mid)
    # Left column ends at 289, right column starts at 313: a 24pt gutter.
    two_cols = _raw_line([(40, 120), (280, 289), (313, 330), (500, 550)])
    assert not _is_full_width(two_cols, mid)
    # A line entirely in one column is never full width.
    assert not _is_full_width(_raw_line([(40, 120), (200, 280)]), mid)


def test_query_terms_turn_ordinals_into_numbers():
    assert query_terms("What does the 10th spot do in the 2nd age?") == ["10", "spot", "2", "age"]


def test_query_terms_drop_stopwords():
    assert query_terms("Can I build a settlement next to a city?") == ["build", "settlement", "next", "city"]


def test_expand_terms_adds_rulebook_words_including_plurals():
    groups = expand_terms("catan", ["thief", "ports", "settlement"])
    assert groups == [["thief", "robber"], ["ports", "harbor"], ["settlement"]]
    assert expand_terms("unknown-game", ["thief"]) == [["thief"]]



def _box_line(words: list[tuple[str, float]], top: float, size: float = 10.0) -> dict:
    """A box line as _box_lines builds it, from (word, x0) pairs at 5pt per letter."""
    runs = [[{"text": ch, "x0": x + 5 * i, "x1": x + 5 * (i + 1), "top": top, "bottom": top + size, "size": size}
             for i, ch in enumerate(w)] for w, x in words]
    chars = [c for r in runs for c in r]
    return {"text": " ".join(w for w, _ in words), "chars": chars, "runs": runs,
            "x0": chars[0]["x0"], "x1": chars[-1]["x1"], "top": top, "bottom": top + size}


def test_inline_boxes_merge_split_off_words_back_into_their_line():
    # "Separate the Wild Location [icon] and Wild Industry [icon] cards and place":
    # pdfminer puts "and" and "cards and place" in their own boxes over the paragraph's.
    paragraph = [_box_line([("Separate", 0), ("Wild", 50), ("Location", 75)], top=0),
                 _box_line([("Wild", 0), ("Industry", 25)], top=11),
                 _box_line([("them", 0), ("face", 25), ("up.", 50)], top=22)]
    after_icon = [_box_line([("and", 125)], top=0)]  # past an icon at the line's end
    cards = [_box_line([("cards", 75), ("and", 105), ("place", 125)], top=11)]
    callout = [_box_line([("C", 40)], top=22)]  # a diagram label: stays out
    sidebar = [_box_line([("Note", 400)], top=0)]  # another column: stays out
    boxes = _merge_inline([paragraph, after_icon, cards, callout, sidebar])
    assert [ln["text"] for ln in boxes[0]] == ["Separate Wild Location and", "Wild Industry cards and place", "them face up."]
    assert [b[0]["text"] for b in boxes[1:]] == ["C", "Note"]
