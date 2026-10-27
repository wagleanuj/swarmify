"""Learning context: how a world gets better run over run (ADR 0008).

Skills are versioned procedures agents load when they match a stage; every version is stored
with the run that produced it, and the current SKILL.md lives in the world folder (shared skills
in `<data>/skills`, the world's own winning on conflict). The Retro is the stage after every run:
a judge-style session over the run's verdicts, send-backs and gate notes and the earlier retros,
proposing at most one skill change with evidence, plus a workflow change when the same manual
intervention recurred in three runs. A proposal waits at a gate; approval writes a skill version,
rejection records why. The Scorecard is a read model over events, no storage.

Learning reads events and files; it changes behaviour only through that gate, which Runs owns.
"""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from swarmify_kernel import Event, new_id
from swarmify_learning.model import (
    ACTIONS,
    SKILL_NAME,
    Proposal,
    RunScore,
    Skill,
    SkillChange,
    SkillVersion,
    WorkflowSuggestion,
)
from swarmify_learning.ports import Ask, Storage
from swarmify_learning.retro import (
    RECURRENCE,
    RETRO_SCHEMA,
    SYSTEM,
    Evidence,
    Recurrence,
    Retro,
    check_skill,
    evidence,
    parse_retro,
    prompt,
    proposal_markdown,
    recurrences,
)
from swarmify_learning.scorecard import scorecard
from swarmify_learning.skills import SkillFiles
from swarmify_learning.sqlite import MIGRATIONS, SqliteStorage

__all__ = [
    "ACTIONS",
    "MIGRATIONS",
    "RECURRENCE",
    "RETRO_SCHEMA",
    "SKILL_NAME",
    "Ask",
    "Evidence",
    "Learning",
    "Proposal",
    "Recurrence",
    "Retro",
    "RunScore",
    "Skill",
    "SkillChange",
    "SkillFiles",
    "SkillVersion",
    "SqliteStorage",
    "Storage",
    "WorkflowSuggestion",
    "check_skill",
    "evidence",
    "parse_retro",
    "recurrences",
    "scorecard",
]

RETRO = "retro"
"""The agent name the retro session runs under."""
PRIORS = 8
"""How many earlier proposals the retro reads."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Learning:
    def __init__(self, storage: Storage, data_dir: Path) -> None:
        self.storage = storage
        self.files = SkillFiles(data_dir)
        self.data_dir = data_dir

    # Skills

    def sync(self, world_id: str) -> None:
        """Make the shared skills the world has not overridden loadable from its folder. Call it
        before agents start."""
        self.files.sync(world_id)

    def skills(self, world_id: str) -> list[Skill]:
        """Every skill the world's agents load, with the world's version and run for its own."""
        latest = {v.name: v for v in self.storage.versions(world_id)}
        out = []
        for skill in self.files.list(world_id):
            v = latest.get(skill.name) if skill.source == "world" else None
            if v is not None and not v.retired:
                skill = replace(skill, version=v.number, run_id=v.run_id, created_at=v.created_at)
            out.append(skill)
        return out

    def versions(self, world_id: str, name: str) -> list[SkillVersion]:
        return self.storage.versions(world_id, name)

    def write_skill(
        self, world_id: str, name: str, content: str, run_id: str | None = None, note: str = ""
    ) -> Skill:
        """Write the world's skill as its next version (1 for a new one) and make it current.
        Raises ValueError for a name or content the agents would not load."""
        check_skill(name, content)
        self._version(world_id, name, content, run_id, note, retired=False)
        self.files.write(world_id, name, content)
        return next(s for s in self.skills(world_id) if s.name == name)

    def retire_skill(
        self, world_id: str, name: str, run_id: str | None = None, note: str = ""
    ) -> SkillVersion:
        """Remove the world's own skill; a shared one of that name applies again."""
        if self.files.read(world_id, name) is None:
            raise ValueError(f"the world has no skill named {name}")
        version = self._version(world_id, name, "", run_id, note, retired=True)
        self.files.remove(world_id, name)
        self.files.sync(world_id)
        return version

    def _version(
        self, world_id: str, name: str, content: str, run_id: str | None, note: str, retired: bool
    ) -> SkillVersion:
        earlier = self.storage.versions(world_id, name)
        number = earlier[-1].number + 1 if earlier else 1
        version = SkillVersion(
            new_id("skill"), world_id, name, number, content, run_id, note, retired, _now()
        )
        payload = {k: v for k, v in asdict(version).items() if k != "content"}
        event = Event.new("skill.updated", world_id=world_id, run_id=run_id, payload=payload)
        return self.storage.add_version(version, event)

    # Retro

    def retro(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        workflow_id: str | None,
        workflow_yaml: str | None,
        events: list[Event],
        ask: Ask,
    ) -> Proposal | None:
        """Run the retro session over the run's evidence. Returns the proposal it recorded, or
        None when it proposed nothing. A proposal is also a `proposal.md` artifact of the stage."""
        run = evidence(events, run_id)
        recurring = recurrences(events, workflow_id) if workflow_id else []
        skills = [(s.name, s.content) for s in self.skills(world_id)]
        priors = [
            f"{p.created_at[:10]} run {p.run_id}: {_summary(p)} ({p.status}"
            + (f", {p.note}" if p.note else "")
            + ")"
            for p in self.storage.list_proposals(world_id)[-PRIORS:]
        ]
        text = prompt(run, recurring, skills, priors, workflow_yaml)
        retro: Retro = ask(SYSTEM, text, RETRO_SCHEMA, parse_retro)
        skill = self._settle(retro.skill, {name for name, _ in skills})
        # A workflow change is only ever drawn from a recurrence Learning itself found.
        workflow = retro.workflow if recurring else None
        if skill is None and workflow is None:
            return None
        return self._propose(world_id, run_id, stage_id, skill, workflow)

    @staticmethod
    def _settle(skill: SkillChange | None, names: set[str]) -> SkillChange | None:
        """The action against what the world has: a new skill that exists is a change, a change
        to one that does not is new, and retiring what is not there is nothing."""
        if skill is None:
            return None
        if skill.action == "retired":
            return skill if skill.name in names else None
        action = "changed" if skill.name in names else "new"
        return replace(skill, action=action)

    def _propose(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        skill: SkillChange | None,
        workflow: WorkflowSuggestion | None,
    ) -> Proposal:
        folder = self.data_dir / "worlds" / world_id / "runs" / run_id / stage_id / "artifacts"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "proposal.md"
        proposal = Proposal(
            new_id("prop"),
            world_id,
            run_id,
            stage_id,
            skill,
            workflow,
            "pending",
            "",
            str(path.relative_to(self.data_dir / "worlds" / world_id)),
            _now(),
        )
        front = {
            "type": "skill_proposal",
            "author": RETRO,
            "run": run_id,
            "stage": stage_id,
            "created": proposal.created_at,
            "title": _summary(proposal),
            "skill": skill.name if skill else None,
            "action": skill.action if skill else None,
        }
        lines = [f"{k}: {json.dumps(v)}" for k, v in front.items() if v is not None]
        path.write_text(
            "---\n" + "\n".join(lines) + "\n---\n\n" + proposal_markdown(skill, workflow)
        )
        event = Event.new(
            "proposal.created",
            world_id=world_id,
            run_id=run_id,
            payload={"id": proposal.id, "run_id": run_id, "summary": _summary(proposal)},
        )
        return self.storage.add_proposal(proposal, event)

    def pending(self, run_id: str) -> Proposal | None:
        return self.storage.pending(run_id)

    def proposal(self, proposal_id: str) -> Proposal | None:
        return self.storage.get_proposal(proposal_id)

    def proposals(self, world_id: str) -> list[Proposal]:
        return self.storage.list_proposals(world_id)

    def decide(
        self, proposal_id: str, approve: bool, content: str | None = None, note: str = ""
    ) -> Proposal:
        """Approve the proposal, writing its skill (as edited to `content` when given) as a new
        version of the world's skills, or reject it with `note` saying why. Raises KeyError for
        an unknown proposal, ValueError when it was decided already or the edited skill is not
        loadable. The run's gate is Runs' to answer."""
        proposal = self.storage.get_proposal(proposal_id)
        if proposal is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        if proposal.status != "pending":
            raise ValueError(f"the proposal was already {proposal.status}")
        note = note.strip()
        if approve and proposal.skill is not None:
            skill = proposal.skill
            if skill.action == "retired":
                self.retire_skill(proposal.world_id, skill.name, proposal.run_id, note)
            else:
                text = content if content is not None else skill.content
                check_skill(skill.name, text)
                self.write_skill(proposal.world_id, skill.name, text, proposal.run_id, note)
        status = "approved" if approve else "rejected"
        event = Event.new(
            "proposal.decided",
            world_id=proposal.world_id,
            run_id=proposal.run_id,
            payload={"id": proposal_id, "status": status, "note": note},
        )
        decided = self.storage.decide_proposal(proposal_id, status, note, _now(), event)
        if decided is None:
            raise ValueError("the proposal was decided meanwhile")
        return decided

    # Scorecard

    def scorecard(self, events: list[Event], workflow_id: str) -> list[RunScore]:
        return scorecard(events, workflow_id)

    def delete_world(self, world_id: str) -> None:
        self.storage.delete_world(world_id)


def _summary(p: Proposal) -> str:
    parts = []
    if p.skill:
        parts.append(f"{p.skill.action} skill {p.skill.name}: {p.skill.summary}")
    if p.workflow:
        parts.append(f"workflow: {p.workflow.summary}")
    return "; ".join(parts) or "nothing proposed"
