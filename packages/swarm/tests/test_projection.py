"""The planner's and judge's answers, parsed, and a passing verdict projected into artifacts."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swarmify_swarm.projection import (
    Item,
    OutputType,
    Source,
    notes_for_judge,
    output_type,
    paragraphs,
    parse_plan,
    parse_verdict,
    project,
    verdict_file,
)

NAMES = st.from_regex(r"[a-z][a-z0-9_]{0,12}", fullmatch=True)


@given(NAMES)
def test_output_types_parse_as_one_or_a_list(name: str) -> None:
    assert output_type(name) == OutputType(name, False)
    assert output_type(f"list[{name}]") == OutputType(name, True)
    assert output_type(f" list[ {name} ] ") == OutputType(name, True)


@pytest.mark.parametrize("bad", ["list[idea", "Idea", "list[list[idea]]", "idea card", "[idea]"])
def test_a_malformed_output_type_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="not an output type"):
        output_type(bad)


def test_no_output_type_is_none() -> None:
    assert output_type(None) is None and output_type("  ") is None


def test_a_plan_keeps_only_dependencies_on_earlier_tasks() -> None:
    seeds = parse_plan(
        {"tasks": [{"title": " A "}, {"title": "B", "body": "b", "after": [0, 0, 1, 7, -1, "x"]}]}
    )
    assert [(s.title, s.body, s.after) for s in seeds] == [("A", "", ()), ("B", "b", (0,))]
    for bad in (None, [], {"tasks": []}, {"tasks": [{"title": ""}]}, {"tasks": ["x"]}):
        with pytest.raises(ValueError):
            parse_plan(bad)


def test_a_fail_always_names_gaps_and_a_pass_never_does() -> None:
    fail = parse_verdict({"passed": False, "reasons": ["thin", " "], "items": [{"title": "x"}]})
    assert (fail.gaps, fail.items, fail.reasons) == (["Close the gap: thin"], [], ["thin"])
    named = parse_verdict({"passed": False, "reasons": ["thin"], "gaps": ["Find 2 more"]})
    assert named.gaps == ["Find 2 more"]
    ok = parse_verdict(
        {
            "passed": True,
            "reasons": ["ok"],
            "gaps": ["ignored"],
            "items": [
                {
                    "title": "T",
                    "body": "B",
                    "rank": "1",
                    "sources": [{"note": "n", "paragraph": "2"}, {"note": "n", "paragraph": 2}],
                }
            ],
        }
    )
    assert ok.gaps == [] and ok.items == [Item("T", "B", None, {}, [Source("n", 2)])]
    for bad in (None, {"passed": "yes", "reasons": ["r"]}, {"passed": True, "reasons": []}):
        with pytest.raises(ValueError):
            parse_verdict(bad)


def test_paragraphs_are_blocks_between_blank_lines() -> None:
    assert paragraphs("a\nb\n\n \n\nc\n") == ["a\nb", "c"]
    assert notes_for_judge([("n.md", "x\n\ny")]) == "## n.md\n[1] x\n[2] y"
    assert notes_for_judge([]) == "(no notes were written)"


NOTES = [("runs/r/s/notes/a.md", "One.\n\nTwo."), ("runs/r/s/notes/b.md", "Three.")]


def test_projection_writes_typed_files_with_valid_sources_only() -> None:
    items = [
        Item(
            "Same", "x", 2, {"hook": "h"}, [Source("runs/r/s/notes/a.md", 2), Source("nope.md", 1)]
        ),
        Item(
            "Same", "y", 1, {}, [Source("runs/r/s/notes/b.md", 1), Source("runs/r/s/notes/b.md", 2)]
        ),
    ]
    first, second = project(OutputType("idea", True), items, NOTES)
    assert (first.name, second.name) == ("idea-same.md", "idea-same-2.md")
    assert first.fields == {
        "hook": "h",
        "title": "Same",
        "rank": 2,
        "sources": ["runs/r/s/notes/a.md#p2"],
    }
    assert 'runs/r/s/notes/a.md paragraph 2: "Two."' in first.body and "**hook**: h" in first.body
    assert second.fields["sources"] == ["runs/r/s/notes/b.md#p1"]
    (only,) = project(OutputType("document", False), items, NOTES)
    assert (only.name, only.type, only.body.splitlines()[2]) == (
        "document-same.md",
        "document",
        "y",
    )


@given(
    st.lists(
        st.tuples(
            st.sampled_from(["runs/r/s/notes/a.md", "runs/r/s/notes/b.md", "x.md"]),
            st.integers(-2, 4),
        ),
        max_size=8,
    )
)
def test_every_projected_source_points_at_a_real_paragraph(refs: list[tuple[str, int]]) -> None:
    (out,) = project(
        OutputType("idea", True), [Item("I", "", None, {}, [Source(*r) for r in refs])], NOTES
    )
    paras = {p: paragraphs(b) for p, b in NOTES}
    for ref in out.fields["sources"]:
        path, _, n = ref.rpartition("#p")
        assert path in paras and 1 <= int(n) <= len(paras[path])


def test_the_verdict_file_carries_outcome_reasons_and_gaps() -> None:
    v = verdict_file(2, parse_verdict({"passed": False, "reasons": ["thin"], "gaps": ["More"]}))
    assert (v.name, v.type, v.fields["passed"], v.fields["loop"]) == (
        "verdict-2.md",
        "verdict",
        False,
        2,
    )
    assert v.body.startswith("# Verdict 2: fail") and "- thin" in v.body and "- More" in v.body
