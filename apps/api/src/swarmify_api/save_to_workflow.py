"""Also save to the workflow (Run Assistant spec, ADR 0027): an applied Run Change becomes the
workflow's next version, so the next run has the fix too. A Stage Edit, Skip or Restructure is
applied to the workflow's current version by stage name (`Workflows.save_from_run`) and shown as
the check's draft first; a workflow changed in those stages since the run's version needs a
second confirmation. An Artifact Edit has no workflow form: the Run Assistant drafts a Stage Edit
to the stage that made the file, which the operator saves instead."""

import json
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from swarmify_api.assistant import MAX_READ, Conversations, Reader, run_turn
from swarmify_memory import GraphIndex
from swarmify_runs import AppliedChange, Run, Runner
from swarmify_swarm import AgentRuntime, SessionEnd, Storage
from swarmify_workflows import Violation, Workflows, WorkflowVersion
from swarmify_worlds import Worlds

log = logging.getLogger(__name__)
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "Why this change, in a sentence or two"},
        "config": {
            "type": "object",
            "description": "Only the fields of the stage's config to change, with their new values",
        },
    },
    "required": ["reply", "config"],
}
SYSTEM = """You are the Run Assistant of swarmify. The operator fixed a file a stage of this run
made and wants the workflow to make it right the first time from now on. Read the run if you need
to (get_run, read_run_file, tail_stage_log, list_run_events, list_workflow_runs, query_graph),
then draft a Stage Edit to the stage that made the file: for a swarm stage its `goal` or
`done_when`, for a connector stage its `params`. Answer only with the JSON the schema asks for:
`config` holds only the fields to change. Nothing is saved until the operator saves it."""


class SaveToWorkflow(BaseModel):
    save: bool = False
    """Save the draft as the workflow's next version; left out, only draft it."""
    confirmed: bool = False
    """Save even though the workflow changed the touched stages since the run's version."""
    version: int | None = None
    """The workflow's version the shown draft applied the change to: saving refuses (409) once
    the workflow moved on from it, so what is saved is what the operator saw."""
    config: dict[str, Any] | None = None
    """For an Artifact Edit: the fields of the stage's config to change, as the draft gave them
    (the operator may edit them); needed to save. Left out, the Run Assistant drafts them."""


class WorkflowDraft(BaseModel):
    """The workflow's next version with the change in it, as the check sees it. `stage` is the
    stage it changes; `saved` the new version once saved."""

    stage: str
    workflow_id: str
    version: int
    """The current version the change applies to."""
    run_version: int
    before: str
    yaml: str
    violations: list[Violation]
    conflicts: list[str]
    """How the current version differs from the run's in the stages the change touches: saving
    needs `confirmed` while there are any."""
    saved: WorkflowVersion | None = None
    config: dict[str, Any] | None = None
    """For an Artifact Edit: the Stage Edit's fields, as drafted, to save."""
    reply: str = ""
    """For an Artifact Edit: the assistant's reason for its draft."""


def stages(run: Run, change: AppliedChange, config: dict[str, Any] | None) -> tuple[list, list]:
    """The stages a change replaces and the ones it puts in their place, in the workflow's
    stage shape; for an Artifact Edit, the Stage Edit of `config` to the stage that made it."""
    stage = run.stages[change.stage_index]
    one = {"name": stage.name, "type": stage.type}
    if change.kind == "stage_edit":
        return [{**one, **change.before}], [{**one, **change.payload}]
    if change.kind == "skip":
        return [one], []
    if change.kind == "restructure":
        return change.before["stages"], change.payload["stages"]
    return [{**one, **stage.config}], [{**one, **stage.config, **(config or {})}]


def router(
    runner: Runner,
    worlds: Worlds,
    workflows: Workflows,
    board: Storage,
    index: GraphIndex,
    runtime: AgentRuntime,
    conversations: Conversations,
) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["runs"])

    async def drafted(run: Run, change: AppliedChange) -> tuple[dict[str, Any], str]:
        """The Run Assistant's Stage Edit for an Artifact Edit: its fields and its reason. The
        turn stays in the run's conversation and its cost on the world's month."""
        stage = run.stages[change.stage_index]
        reader = Reader(run.id, runner, worlds, workflows, board, index, conversations)
        tools = [t for t in reader.tools() if t.name != "propose_change"]
        path = change.payload["path"]
        prompt = (
            f"The operator rewrote {path}, which the stage {stage.name} (id {stage.id}) made."
            f"\n\nBefore:\n{change.before['text'][:MAX_READ]}\n\nAfter:\n"
            f"{change.payload['text'][:MAX_READ]}\n\n{stage.name} is a {stage.type} stage with"
            f" this config:\n{json.dumps(stage.config)}"
        )
        system = f"{SYSTEM}\n\nThe run is {run.name}, id {run.id}."
        end: SessionEnd | None = None
        try:
            end, sid = await run_turn(
                runtime, worlds, conversations, run, system, prompt, tools, DRAFT_SCHEMA
            )
            out = end.output
            if end.is_error or not isinstance(out, dict) or not isinstance(out.get("config"), dict):
                raise RuntimeError(end.result or "the assistant drafted no config")
        except Exception as exc:
            if end is not None:
                conversations.failed(run, str(exc), end.cost_usd)
            log.warning("drafting a save to the workflow failed on run %s: %s", run.id, exc)
            raise HTTPException(502, f"The assistant did not draft a change: {exc}") from exc
        reply = str(out.get("reply") or "").strip()
        asked = f"Also save the edit of {path} to the workflow."
        conversations.turn(run, asked, reply, [], [], [], sid, end.cost_usd)
        return out["config"], reply

    @api.post(
        "/runs/{run_id}/changes/{change_id}/save-to-workflow",
        responses={404: {}, 409: {}, 422: {}, 502: {}},
    )
    async def save_to_workflow(run_id: str, change_id: str, body: SaveToWorkflow) -> WorkflowDraft:
        """Draft an applied Run Change as the workflow's next version, matched by stage name,
        and with `save` save it. An Artifact Edit is drafted by the Run Assistant as a Stage
        Edit to the stage that made the file (502 when it does not answer), and saved with that
        `config`. 409 for a Rerun or an Ad Hoc Run, which have no workflow form, or when saving
        a workflow changed since the run's version without `confirmed` or since the draft's
        `version`; 422 when the check refuses the draft."""
        run = runner.get(run_id)
        change = next((c for c in run.changes if c.id == change_id), None) if run else None
        if run is None or change is None:
            raise HTTPException(404, "change not found")
        if change.kind == "rerun":
            raise HTTPException(409, "a Rerun changes nothing a workflow holds")
        if run.workflow_id is None or run.workflow_version is None:
            raise HTTPException(409, "an Ad Hoc Run has no workflow to save to")
        config = body.config if change.kind == "artifact_edit" else None
        reply = ""
        if change.kind == "artifact_edit" and config is None:
            if body.save:
                raise HTTPException(422, "save the Stage Edit the draft gave as config")
            config, reply = await drafted(run, change)
        workflow = workflows.get(run.workflow_id)
        if body.save and workflow and body.version not in (None, workflow.current_version):
            raise HTTPException(409, "The workflow moved on since this draft; draft it again.")
        before, after = stages(run, change, config)
        try:
            draft = workflows.save_from_run(
                run.workflow_id,
                run.workflow_version,
                before,
                after,
                save=body.save,
                confirmed=body.confirmed,
            )
        except KeyError as exc:
            raise HTTPException(404, "workflow not found") from exc
        if body.save and draft.saved is None:
            if draft.violations:
                raise HTTPException(422, "; ".join(v.message for v in draft.violations))
            raise HTTPException(409, " ".join(draft.conflicts))
        stage = change.stage_name
        return WorkflowDraft(stage=stage, config=config, reply=reply, **asdict(draft))

    return api
