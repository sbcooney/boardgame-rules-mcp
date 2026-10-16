"""The parts of evals/e2e.py that run without a model."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("e2e", Path(__file__).parents[1] / "evals" / "e2e.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)

RULEBOOK = "the robber begins the game in the desert. it is moved only by rolling a \"7\" or playing a knight card."


def test_fabricated_quotes_flags_only_invented_text():
    answer = (
        "Per Almanac > Robber (p. 11): “The robber begins the game in the desert.” "
        'Also "It is moved only by rolling a 7 or playing a Knight card" and '
        "“The robber may never be moved back to the desert hex”."
    )
    checked, bad = e2e.fabricated_quotes(answer, RULEBOOK)
    assert checked == 3
    # Writing 7 for the rulebook's "7" is not a fabrication; the invented sentence is.
    assert bad == ["The robber may never be moved back to the desert hex"]


def test_markdown_brackets_and_blockquotes_are_not_fabrications():
    rulebook = 'you can lose three ways: • too much blight: the card often says "you lose". start with exactly 1 additional on the card.'
    answer = (
        '> "You can lose three ways:\n> • **Too Much Blight:** the card often says "you lose"."\n\n'
        'The change: "Start with exactly 1 additional [Blight] on the card." '
        'But not "Start with exactly 2 additional [Blight] on the card."'
    )
    checked, bad = e2e.fabricated_quotes(answer, rulebook)
    assert checked == 3
    assert bad == ["Start with exactly 2 additional [Blight] on the card."]


def test_attributions_paths_and_pdf_spacing_are_not_fabrications():
    rulebook = "you may build roads and/ or cities (see the costs card ). green heron trade 1 food for any other type."
    answer = (
        '> "You may build roads and/or cities (see the costs card)."\n> — Almanac > Build, p. 3\n\n'
        'See "The Four Actions > Option 1. Play a Bird From Your Hand". '
        '"Green Heron — Trade 1 food for any other type."'
    )
    assert e2e.fabricated_quotes(answer, rulebook) == (2, [])


def test_blockquote_citation_after_the_quote_is_not_part_of_it():
    rulebook = "each player takes a tray and places it face-up in front of them."
    answer = '> "Each player takes a tray and places it face-up in front of them." (Set Up, p. 2)'
    assert e2e.fabricated_quotes(answer, rulebook) == (1, [])


def test_short_quotes_are_ignored():
    checked, bad = e2e.fabricated_quotes("It says “move the robber” here.", RULEBOOK)
    assert (checked, bad) == (0, [])


def test_parse_grade_tolerates_code_fences():
    g = e2e.parse_grade('```json\n{"ruling": "correct", "abstained": false, "cited_sections": ["Robber"], "note": ""}\n```')
    assert g["ruling"] == "correct"


def test_cited_correctly_matches_by_section_name():
    expected = ["Almanac > Rolling a “7” and Activating the Robber", "Almanac > Knight Cards"]
    assert e2e.cited_correctly(["Knight Cards (p. 8)"], expected)
    assert not e2e.cited_correctly(["Robber"], ["Almanac > Distance Rule"])


def test_isolation_checks():
    e2e.check_isolation("memory", [])
    with pytest.raises(RuntimeError):
        e2e.check_isolation("memory", ["mcp__bgrules__search_rules"])
    e2e.check_isolation("server", ["mcp__bgrules__search_rules"])
    with pytest.raises(RuntimeError):
        e2e.check_isolation("server", [])
    with pytest.raises(RuntimeError):
        e2e.check_isolation("server", ["mcp__bgrules__search_rules", "WebSearch"])
