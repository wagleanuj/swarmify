"""The planner's and judge's answers, parsed, and a passing verdict projected into artifacts."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swarmify_swarm.projection import (
    VERDICT_SCHEMA,
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
    verdict_schema,
)

NAMES = st.from_regex(r"[a-z][a-z0-9_]{0,12}", fullmatch=True)


@given(NAMES)
def test_output_types_parse_as_one_or_a_list(name: str) -> None:
    assert output_type(name) == OutputType(name, False)
    assert output_type(f"list[{name}]") == OutputType(name, True)
    assert output_type(f" list[ {name} ] ") == OutputType(name, True)


@pytest.mark.parametrize(
    "bad", ["list[idea", "Idea", "list[list[idea]]", "idea card", "[idea]", "i" * 41]
)
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
                    "reason": " Strongest hook. ",
                    "sources": [{"note": "n", "paragraph": "2"}, {"note": "n", "paragraph": 2}],
                }
            ],
        }
    )
    assert ok.gaps == [] and ok.items == [
        Item("T", "B", None, {}, [Source("n", 2)], "Strongest hook.")
    ]
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
            "Same",
            "x",
            2,
            {"hook": "h"},
            [Source("runs/r/s/notes/a.md", 2), Source("nope.md", 1)],
            "Second: a weaker hook.",
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
        "reason": "Second: a weaker hook.",
        "sources": ["runs/r/s/notes/a.md#p2"],
    }
    assert "reason" not in second.fields  # none given
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


# A type a connector declares (issue 133): a fixture, nothing video about it.
RECIPE = {
    "type": "object",
    "properties": {"steps": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
    "required": ["steps"],
}


def test_a_declared_type_replaces_the_generic_fields_in_the_judges_schema() -> None:
    assert verdict_schema(None) is VERDICT_SCHEMA
    item = verdict_schema(RECIPE)["properties"]["items"]["items"]
    assert item["properties"]["fields"] == RECIPE and "fields" in item["required"]
    assert VERDICT_SCHEMA["properties"]["items"]["items"]["properties"]["fields"]["type"] == (
        "object"
    )  # the generic schema is left as it was


def test_an_item_that_does_not_fit_its_declared_type_is_refused() -> None:
    def verdict(fields: object) -> dict:
        return {
            "passed": True,
            "reasons": ["ok"],
            "items": [{"title": "Soup", "body": "b", "sources": [], "fields": fields}],
        }

    (item,) = parse_verdict(verdict({"steps": ["Boil."]}), RECIPE).items
    assert item.fields == {"steps": ["Boil."]}
    with pytest.raises(ValueError, match=r"item 1 does not fit its type at steps: \[\] should"):
        parse_verdict(verdict({"steps": []}), RECIPE)
    with pytest.raises(ValueError, match="at the top: 'steps' is a required property"):
        parse_verdict(verdict({}), RECIPE)


def test_a_declared_type_is_written_as_the_produced_file_with_a_fenced_json_block() -> None:
    soup = Item("Soup", "Boil, then season.", 1, {"steps": ["Boil.", "Season."]})
    (a,) = project(OutputType("recipe", False), [soup], [], True, ["notes.md", "recipe.md"])
    assert a.name == "recipe.md" and a.type == "recipe"
    assert a.fields == {"title": "Soup", "rank": 1, "sources": []}
    fenced = a.body.split("```json\n")[1].split("\n```")[0]
    assert json.loads(fenced) == {"steps": ["Boil.", "Season."]}
    assert a.body.index("```json") < a.body.index("Boil, then season.")
    (b,) = project(OutputType("recipe", False), [soup], [], True, ["draft.md"])
    assert b.name == "draft.md"  # no file named after the type: the first produced one
    (c,) = project(OutputType("recipe", False), [soup], [], True, [])
    assert c.name == "recipe-soup.md"
    many = project(OutputType("recipe", True), [soup, soup], [], True, ["recipe.md"])
    assert [m.name for m in many] == ["recipe-soup.md", "recipe-soup-2.md"]


def test_a_declared_types_top_level_text_reaches_the_frontmatter() -> None:
    # A publish stage reads title and description from the produced file's frontmatter.
    item = Item(
        "Kidneys",
        "Body.",
        1,
        {"description": "Why you can have five.", "scenes": [{"id": "s1"}], "rank": "x"},
    )
    (a,) = project(OutputType("script", False), [item], [], True, ["script.md"])
    assert a.fields["description"] == "Why you can have five."
    assert (
        a.fields["title"] == "Kidneys" and a.fields["rank"] == 1
    )  # reserved keys are never overwritten
    assert "scenes" not in a.fields
