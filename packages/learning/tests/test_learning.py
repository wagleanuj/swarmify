"""The Learning service against real SQLite in a temp folder and a fake retro session: skill
versions and files, shared skills linked in with the world winning, the retro's proposal and
its approval or rejection."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from swarmify_kernel import Event
from swarmify_learning import Learning, Proposal

SKILL = "---\nname: hooks\ndescription: Open on the hook.\n---\n\nWrite the hook first.\n"
SHARED = "---\nname: cite\ndescription: Cite sources.\n---\n\nName the note you took it from.\n"
WORLD = "world_1"


class FakeAsk:
    """Answers the retro session with canned output and keeps every prompt."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.schemas: list[dict] = []

    def __call__(self, system: str, prompt: str, schema: dict, parse: Callable[[Any], Any]) -> Any:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        return parse(self.answers.pop(0))


def run_event(run_id: str, decisions: list[dict], workflow_id: str = "wf_1") -> Event:
    payload = {
        "id": run_id,
        "name": "Short",
        "workflow_id": workflow_id,
        "status": "done",
        "send_backs": 0,
        "stages": [{"id": f"{run_id}_s0", "name": "research", "type": "swarm"}],
        "decisions": decisions,
    }
    return Event.new("run.updated", world_id=WORLD, payload=payload, run_id=run_id)


def sent_back(note: str) -> dict:
    return {"id": "d", "stage_name": "review", "kind": "approve", "choice": None, "note": note,
            "send_back_to": "research"}  # fmt: skip


def write_shared(root: Path, name: str, content: str) -> None:
    folder = root / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(content)


def test_skill_versions_land_as_files_the_world_loads_and_shared_ones_link_in(
    learning: Learning, tmp_path: Path
) -> None:
    write_shared(tmp_path, "cite", SHARED)
    write_shared(tmp_path, "hooks", SHARED.replace("cite", "hooks"))
    assert [(s.name, s.source, s.version) for s in learning.skills(WORLD)] == [
        ("cite", "shared", None),
        ("hooks", "shared", None),
    ]
    world_skills = tmp_path / "worlds" / WORLD / "skills"
    assert (world_skills / "cite").is_symlink() and (world_skills / "cite" / "SKILL.md").is_file()

    first = learning.write_skill(WORLD, "hooks", SKILL, run_id="run_1", note="From the retro")
    assert (first.source, first.version, first.run_id) == ("world", 1, "run_1")
    assert first.description == "Open on the hook." and first.path.endswith("hooks/SKILL.md")
    assert not (world_skills / "hooks").is_symlink()
    assert (world_skills / "hooks" / "SKILL.md").read_text() == SKILL
    second = learning.write_skill(WORLD, "hooks", SKILL.replace("first", "early"))
    assert (second.version, second.run_id) == (2, None)
    assert [(v.number, v.run_id, v.note) for v in learning.versions(WORLD, "hooks")] == [
        (1, "run_1", "From the retro"),
        (2, None, ""),
    ]
    with pytest.raises(ValueError, match="frontmatter must say name"):
        learning.write_skill(WORLD, "other", SKILL)
    with pytest.raises(ValueError, match="not a skill name"):
        learning.write_skill(WORLD, "Hooks", SKILL)

    retired = learning.retire_skill(WORLD, "hooks", note="Not needed")
    assert (retired.number, retired.retired) == (3, True)
    assert [(s.name, s.source) for s in learning.skills(WORLD)] == [
        ("cite", "shared"),
        ("hooks", "shared"),  # the shared one applies again
    ]
    with pytest.raises(ValueError):
        learning.retire_skill(WORLD, "nope")

    # A hand-written world skill has no version; a shared skill that vanished is unlinked.
    (world_skills / "hand").mkdir()
    (world_skills / "hand" / "SKILL.md").write_text(SKILL.replace("hooks", "hand"))
    (tmp_path / "skills" / "cite" / "SKILL.md").unlink()
    assert [(s.name, s.source, s.version) for s in learning.skills(WORLD)] == [
        ("hand", "world", None),
        ("hooks", "shared", None),
    ]
    assert not (world_skills / "cite").exists()
    learning.delete_world(WORLD)
    assert learning.versions(WORLD, "hooks") == []


def test_the_retro_proposes_nothing_or_one_evidenced_skill_change_and_the_gate_decides_it(
    learning: Learning, tmp_path: Path
) -> None:
    write_shared(tmp_path, "cite", SHARED)
    events = [run_event("run_1", [sent_back("Too long.")])]
    nothing = FakeAsk({"skill": None, "workflow": None})
    assert learning.retro(WORLD, "run_1", "stage_r", "wf_1", "name: Short", events, nothing) is None
    assert "sent back to research with the note: Too long." in nothing.prompts[0]
    assert "## cite" in nothing.prompts[0] and "name: Short" not in nothing.prompts[0]
    assert nothing.schemas[0]["properties"]["skill"]["properties"]["action"]["enum"] == [
        "new", "changed", "retired"
    ]  # fmt: skip
    assert learning.pending("run_1") is None

    proposal = {
        "skill": {
            "action": "new",
            "name": "cite",
            "content": SHARED.replace("Name", "Quote"),
            "summary": "Quote the note",
            "evidence": ["review: sent back"],
        },  # fmt: skip
    }
    made = learning.retro(WORLD, "run_1", "stage_r", "wf_1", None, events, FakeAsk(proposal))
    assert isinstance(made, Proposal) and made.status == "pending" and made.workflow is None
    assert made.skill is not None and made.skill.action == "changed"  # cite exists (shared)
    assert learning.pending("run_1") == made
    file = tmp_path / "worlds" / WORLD / made.path
    assert made.path == "runs/run_1/stage_r/artifacts/proposal.md"
    text = file.read_text()
    assert text.startswith('---\ntype: "skill_proposal"\nauthor: "retro"\n')
    assert (
        "## Skill cite: changed" in text and "- review: sent back" in text and "Quote the" in text
    )

    edited = SHARED.replace("Name the note", "Quote the paragraph")
    decided = learning.decide(made.id, approve=True, content=edited, note="Tightened the wording")
    assert decided.status == "approved" and decided.decided_at and decided.note.startswith("Tight")
    (cite,) = [s for s in learning.skills(WORLD) if s.name == "cite"]
    assert (cite.source, cite.version, cite.run_id) == ("world", 1, "run_1")
    assert cite.content == edited and learning.pending("run_1") is None
    with pytest.raises(ValueError, match="already approved"):
        learning.decide(made.id, approve=False)
    with pytest.raises(KeyError):
        learning.decide("prop_nope", approve=True)

    # Earlier retros reach the next one; a rejection records why and writes nothing.
    later = FakeAsk(proposal)
    again = learning.retro(WORLD, "run_2", "stage_r2", "wf_1", None, events, later)
    assert again is not None and "changed skill cite: Quote the note (approved" in later.prompts[0]
    with pytest.raises(ValueError, match="frontmatter"):
        learning.decide(again.id, approve=True, content="broken")
    rejected = learning.decide(again.id, approve=False, note="Not the problem")
    assert (rejected.status, rejected.note) == ("rejected", "Not the problem")
    assert learning.versions(WORLD, "cite")[-1].number == 1
    assert [p.status for p in learning.proposals(WORLD)] == ["approved", "rejected"]

    # Retiring through a proposal removes the world's own file; retiring the unknown is nothing.
    retire = {"skill": {"action": "retired", "name": "cite", "evidence": ["unused"]}}
    gone = learning.retro(WORLD, "run_3", "s", "wf_1", None, events, FakeAsk(retire))
    assert gone is not None and gone.skill is not None and gone.skill.action == "retired"
    learning.decide(gone.id, approve=True)
    assert [(s.name, s.source) for s in learning.skills(WORLD)] == [("cite", "shared")]
    unknown = {"skill": {"action": "retired", "name": "nope", "evidence": ["x"]}}
    assert learning.retro(WORLD, "run_4", "s", "wf_1", None, events, FakeAsk(unknown)) is None


def test_a_recurring_send_back_puts_the_yaml_in_front_of_the_retro_and_keeps_its_suggestion(
    learning: Learning,
) -> None:
    events = [run_event(f"run_{i}", [sent_back("Shorter.")]) for i in range(3)]
    yaml = "name: Short\nstages:\n  - swarm: research\n    goal: g\n    done_when: d\n"
    suggestion = {
        "workflow": {
            "summary": "Ask for a shorter cut",
            "yaml": yaml + "    tools: []\n",
            "evidence": ["send_back at review: research in 3 runs"],
        }
    }
    ask = FakeAsk(suggestion)
    made = learning.retro(WORLD, "run_2", "stage_r", "wf_1", yaml, events, ask)
    assert "repeated in 3 or more runs" in ask.prompts[0] and yaml in ask.prompts[0]
    assert made is not None and made.skill is None and made.workflow is not None
    assert made.workflow.summary == "Ask for a shorter cut"
    # Only two runs of another workflow: nothing recurs, so no YAML and no suggestion asked for.
    other = [run_event(f"run_{i}", [sent_back("Shorter.")], "wf_2") for i in range(2)]
    ask = FakeAsk({"skill": None, "workflow": None})
    assert learning.retro(WORLD, "run_1", "s", "wf_2", yaml, other, ask) is None
    assert "repeated in" not in ask.prompts[0]
