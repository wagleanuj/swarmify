"""HTTP face of the Learning context: a world's skills (its own versions and the shared ones it
loads), the retro proposal a run waits on and its decision, and a workflow's scorecard."""

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from swarmify_kernel import Event
from swarmify_learning import Learning, Proposal, RunScore, Skill, SkillVersion
from swarmify_learning import scorecard as scorecard_of
from swarmify_runs import Illegal, RunControl
from swarmify_workflows import Invalid, Workflows
from swarmify_worlds import Worlds

Events = Callable[[str], list[Event]]
"""(world id) to the world's whole event stream, which the scorecard and the retro fold."""


class NewSkill(BaseModel):
    """A skill version to write: a Library artifact promoted to a skill draft and edited, or a
    hand-written one. `run_id` is the run the content came from, kept as lineage."""

    name: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=100_000)
    run_id: str | None = None
    note: str = Field(default="", max_length=2_000)


class DecideProposal(BaseModel):
    """The gate's answer. Approving writes the skill (as edited in `content` when given) as a new
    version; `apply_workflow` saves the suggested YAML as the workflow's next version. Rejecting
    records `note` as why."""

    approve: bool
    content: str | None = Field(default=None, max_length=100_000)
    apply_workflow: bool = False
    note: str = Field(default="", max_length=10_000)


@dataclass(frozen=True)
class Scorecard:
    """Per-run measures of one workflow across its runs, oldest first. Cost is the runtime's
    estimate, not billing."""

    workflow_id: str
    world_id: str
    name: str
    runs: list[RunScore]


def router(
    learning: Learning,
    worlds: Worlds,
    workflows: Workflows,
    control: RunControl,
    events: Events,
) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["learning"])

    def world_or_404(world_id: str) -> None:
        if worlds.get(world_id) is None:
            raise HTTPException(404, "world not found")

    @api.get("/worlds/{world_id}/skills")
    def list_skills(world_id: str) -> list[Skill]:
        """Every skill the world's agents load: its own (with version and the run that produced
        it) and the shared ones it has not overridden."""
        world_or_404(world_id)
        return learning.skills(world_id)

    @api.post("/worlds/{world_id}/skills", status_code=201, responses={422: {}})
    def write_skill(world_id: str, body: NewSkill) -> Skill:
        """Write the next version of a skill (version 1 for a new one) into the world's skills
        folder, so every stage that starts from now on loads it."""
        world_or_404(world_id)
        try:
            return learning.write_skill(world_id, body.name, body.content, body.run_id, body.note)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/worlds/{world_id}/skills/{name}/versions")
    def skill_versions(world_id: str, name: str) -> list[SkillVersion]:
        """Every stored version of the skill, oldest first, each with its run."""
        world_or_404(world_id)
        return learning.versions(world_id, name)

    @api.get("/worlds/{world_id}/proposals")
    def list_proposals(world_id: str) -> list[Proposal]:
        """Every retro proposal of the world, oldest first: what was proposed and how it went."""
        world_or_404(world_id)
        return learning.proposals(world_id)

    @api.get("/runs/{run_id}/proposal", responses={404: {}})
    def pending_proposal(run_id: str) -> Proposal:
        """The proposal the run's retro gate waits on."""
        proposal = learning.pending(run_id)
        if proposal is None:
            raise HTTPException(404, "the run has no proposal waiting")
        return proposal

    @api.post("/proposals/{proposal_id}/decide", responses={404: {}, 409: {}, 422: {}})
    def decide(proposal_id: str, body: DecideProposal) -> Proposal:
        """Approve (with edits) or reject the proposal, then answer the run's gate. 422 when the
        edited skill or the suggested workflow would not load; nothing is written then."""
        proposal = learning.proposal(proposal_id)
        if proposal is None:
            raise HTTPException(404, "proposal not found")
        if proposal.status != "pending":
            raise HTTPException(422, f"the proposal was already {proposal.status}")
        workflow_id = None
        if body.approve and body.apply_workflow and proposal.workflow is not None:
            run = control.get(proposal.run_id)
            if run is None or run.workflow_id is None:
                raise HTTPException(422, "the run has no workflow to version")
            draft = workflows.check(proposal.world_id, proposal.workflow.yaml, run.workflow_id)
            if draft.violations or draft.model is None:
                detail = Invalid(draft.violations)
                raise HTTPException(422, f"the suggested workflow is refused: {detail}")
            workflow_id = run.workflow_id
        try:
            decided = learning.decide(proposal_id, body.approve, body.content, body.note)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if workflow_id is not None and proposal.workflow is not None:
            # Checked above and only the decision that won gets here, so this writes once.
            workflows.save(proposal.world_id, proposal.workflow.yaml, workflow_id)
        try:
            control.decide(
                proposal.run_id, "approve" if body.approve else "reject", body.note, proposal=True
            )
        except (KeyError, Illegal) as exc:
            # The skill is written as asked; only the run had already moved on.
            raise HTTPException(409, f"decided, but the run did not take it: {exc}") from exc
        return decided

    @api.get("/workflows/{workflow_id}/scorecard", responses={404: {}})
    def scorecard(workflow_id: str) -> Scorecard:
        """First-try acceptance, send-backs, cost, duration and outcome of every run of the
        workflow, oldest first: whether learning works."""
        workflow = workflows.get(workflow_id)
        if workflow is None:
            raise HTTPException(404, "workflow not found")
        scores = scorecard_of(events(workflow.world_id), workflow_id)
        return Scorecard(workflow_id, workflow.world_id, workflow.name, scores)

    return api
