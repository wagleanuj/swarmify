"""Acting on a run from outside its workflow: decide at a gate, pause, resume, abort, and
propose and apply Run Changes (ADR 0027).

Any process may act. Messages reach the run's DBOS workflow, the single writer of run state,
through a DBOSClient on the shared system database, so the MCP server works without launching
DBOS. A queued run has no workflow thread yet, so abort cancels its enqueued workflow and
writes the terminal state here. The decision artifact is written here too, before the message
is sent, so the record exists even if the run has moved on by the time the message lands.
"""

import json
import logging
import shutil
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dbos import DBOSClient
from sqlalchemy.exc import OperationalError

from swarmify_kernel import Event, new_id
from swarmify_runs import budget
from swarmify_runs.machine import (
    MAX_SEND_BACKS,
    TERMINAL,
    Aborted,
    GateAnswered,
    Illegal,
    MonthSpent,
    Paused,
    Rerun,
    Resumed,
    RunEvent,
    RunState,
    SentBack,
    Skip,
    rerun_again,
    rerun_from,
    transition,
)
from swarmify_runs.model import (
    AppliedChange,
    ChangeProposal,
    Decision,
    Gate,
    Pinned,
    PublishPreview,
    Run,
    RunChange,
    RunLink,
    RunStageView,
    StageDef,
)
from swarmify_runs.ports import Notifier, Storage

log = logging.getLogger(__name__)
CONTROL_TOPIC = "control"
WAKE = {"swarmify": "wake"}
Spend = Callable[[list[str]], float]
"""The estimated agent cost of the given runs, summed. The composition root reads it from the
Swarm context, whose agents carry the cost."""


Pin = Callable[..., Pinned]
"""(workflow id, version number or None for the current one) to that version as Runs starts it.
Raises KeyError for an unknown workflow or version and ValueError when the version fails today's
workflow check, so every run start of a version refuses one that breaks a rule now."""

Monthly = Callable[[str], float | None]
"""(world id) to the world's Monthly Budget, None for no ceiling. The composition root reads
it from the Worlds context."""


class OverBudget(Exception):
    """Raised by a stage call when the stop check stopped its agents: the run's Budget is spent
    (the stage waits at a budget gate instead of failing) or a pause or abort came mid-stage."""


def no_spend(_run_ids: list[str]) -> float:
    return 0.0


APP_NAME = "swarmify"
"""The DBOS application: the API process launches it; the client acts on its behalf."""
SCHEDULE_PREFIX = "wf-"
SCHEDULED_WORKFLOW = "swarmify.scheduled"
"""The DBOS workflow a schedule fires (registered in `runner`); it starts a run of the
workflow named in the schedule's context."""

Previews = Callable[[Run, StageDef, dict[str, Any]], PublishPreview | None]
"""What a stage of a run will publish, with the person's edits at the gate (by field key) over
its own values, or None when it does not publish. Runs cannot tell a publish connector from
another; the Connectors registry can, through the composition root."""


def no_previews(_run: Run, _stage: StageDef, _edits: dict[str, Any]) -> PublishPreview | None:
    return None


Check = Callable[[Run, list[StageDef]], tuple[list[str], list[StageDef]]]
"""(run, its whole stage list with a Run Change in it) to the violations of the workflow check
at the run's pinned package versions, every rule a workflow save meets, and the stages as the
check parsed them, defaults filled in, in the same places. Runs cannot read the rules; the
composition root checks the list as a workflow model through Workflows."""


def no_check(_run: Run, stages: list[StageDef]) -> tuple[list[str], list[StageDef]]:
    return [], stages


TextCheck = Callable[[Run, str, str, str], list[str]]
"""(run, a file's name, its text before, its new text) to why an Artifact Edit of it would be
refused: JSON or YAML that no longer parses, a markdown artifact that drops or changes the type
its frontmatter declared or no longer fits that type at the run's pinned package versions.
Runs cannot read the types; the composition root asks the Connectors registry."""


def no_text_check(_run: Run, _name: str, _old: str, _text: str) -> list[str]:
    return []


TEXT = (".md", ".markdown", ".json", ".yaml", ".yml", ".txt")
"""The files an Artifact Edit rewrites in place; a Rerun remakes the rest (ADR 0027)."""


StageSpend = Callable[[str], dict[str, float]]
"""A run's id to its agents' estimated cost by stage id, for what a Rerun will cost again."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def queue_name(world_id: str) -> str:
    """The world's DBOS queue, which caps how many of its runs hold a slot."""
    return f"runs:{world_id}"


def run_folder(data_dir: Path, run: Run) -> Path:
    return data_dir / "worlds" / run.world_id / "runs" / run.id


def stage_folder(data_dir: Path, run: Run, index: int) -> Path:
    """`<data>/worlds/<world>/runs/<run>/<stage id>`, the folder Memory keeps the stage's notes
    and artifacts in, so the run's record sits together (ADR 0001)."""
    return run_folder(data_dir, run) / run.stages[index].id


def copy_run_files(data_dir: Path, source: Run, run: Run, index: int) -> None:
    """Copy the source run's folder into its fork's, leaving out the folders of the stages at
    or past `index`. Each copied stage folder (its notes, artifacts and decision) is renamed to
    the fork's stage id so it reads back under it (files are memory, ADR 0001)."""
    src, dst = run_folder(data_dir, source), run_folder(data_dir, run)
    if not src.is_dir():
        return
    renamed = {s.id: run.stages[s.index].id for s in source.stages[:index]}
    skip = {source.stages[i].id for i in range(index, len(source.stages))}
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if child.name in skip:
            continue
        target = dst / renamed.get(child.name, child.name)
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copyfile(child, target)


def with_sidecar(path: Path) -> list[Path]:
    """The file and its markdown sidecar, named either way round, when both exist."""
    name = path.name[:-3] if path.suffix == ".md" else path.name + ".md"
    return [path, path.with_name(name)] if name and path.with_name(name).exists() else [path]


def state_of(run: Run) -> RunState:
    return RunState(
        run.status, tuple(s.status for s in run.stages), run.send_backs, run.over_budget
    )


def kinds_of(run: Run) -> tuple[str, ...]:
    return tuple(s.type for s in run.stages)


def ended_event(run: Run) -> Event:
    return Event.new(
        "run.ended",
        world_id=run.world_id,
        run_id=run.id,
        payload={"run_id": run.id, "name": run.name, "status": run.status},
    )


class RunControl:
    def __init__(
        self,
        storage: Storage,
        system_db: Path,
        data_dir: Path,
        notifier: Notifier,
        previews: Previews = no_previews,
        spend: Spend = no_spend,
        monthly: Monthly = lambda _world_id: None,
    ) -> None:
        self.storage = storage
        self.monthly = monthly
        self.system_db = system_db
        self.data_dir = data_dir
        self.notifier = notifier
        self.previews = previews
        self.spend = spend
        self._client: DBOSClient | None = None
        self.pin: Pin | None = None
        """Set by the composition root; a fork of a workflow run re-checks its version with it."""
        self.check: Check = no_check
        """Set by the composition root; every Run Change is checked with it."""
        self.check_text: TextCheck = no_text_check
        """Set by the composition root; every Artifact Edit's new text is checked with it."""
        self.stage_spend: StageSpend = lambda _run_id: {}
        """Set by the composition root; a Rerun's proposal estimates its cost with it."""

    def start(
        self,
        world_id: str,
        workflow_id: str | None,
        version: int | None,
        name: str,
        stages: list[StageDef],
        parent_run_id: str | None = None,
        budget_usd: float | None = None,
        baseline: bool = False,
        workflow_budget_usd: float | None = None,
    ) -> Run:
        """Record a queued run pinned to `version` and enqueue it. Its Budget follows
        `budget_for`: `budget_usd` typed at the start, else the workflow's
        `workflow_budget_usd`. `baseline` runs every swarm stage with one agent and no graph. An
        Ad Hoc Run has no workflow: `workflow_id` and `version` are None."""
        budget_usd = self.budget_for(world_id, budget_usd, workflow_budget_usd, parent_run_id)
        now = datetime.now(UTC).isoformat()
        run = Run(
            new_id("run"), world_id, workflow_id, version, name, "queued", now, now, [],
            parent_run_id=parent_run_id, budget_usd=budget_usd, baseline=baseline,
        )  # fmt: skip
        run = self.storage.add_run(run, stages)
        self._enqueue(run)
        return run

    def budget_for(
        self,
        world_id: str,
        explicit: float | None = None,
        workflow: float | None = None,
        parent_run_id: str | None = None,
    ) -> float | None:
        """The one budget rule for every way a run starts (start, ad hoc, fork, promote,
        schedule, child): `budget.rule` under the world's Monthly Budget this month. Raises
        MonthSpent when nothing is left. A child run gets None: it spends the Budget of the top
        run of its family (ADR 0013)."""
        if parent_run_id is not None:
            return None
        month = datetime.now(UTC).strftime("%Y-%m")
        try:
            return budget.rule(
                explicit,
                workflow,
                self.monthly(world_id),
                self.storage.spent_in_month(world_id, month),
            )
        except ValueError as exc:
            raise MonthSpent(str(exc)) from exc

    def fork(self, run_id: str, index: int) -> Run:
        """Start a Fork: a new run of the same workflow version that copies `run_id` (rows and
        files) up to stage `index` and runs on from there. Raises KeyError for an unknown run,
        ValueError for an index outside the run and Illegal when a stage before `index` has
        not finished."""
        source = self.storage.get_run(run_id)
        if source is None:
            raise KeyError(f"run not found: {run_id}")
        if not 0 <= index < len(source.stages):
            raise ValueError(f"stage {index} is not one of the run's {len(source.stages)} stages")
        unfinished = [s for s in source.stages[:index] if s.status not in ("done", "skipped")]
        if unfinished:
            raise Illegal(
                f"stage {unfinished[0].name} has not finished, so the run cannot fork from"
                f" {source.stages[index].name}"
            )
        if source.workflow_id is not None and self.pin is not None:
            self.pin(source.workflow_id, source.workflow_version)  # today's check, or raises
        run = self.storage.fork(self._new_fork(source), source, index)
        copy_run_files(self.data_dir, source, run, index)
        self._enqueue(run)
        return run

    def _new_fork(self, source: Run) -> Run:
        """The Fork of `source` to insert, queued. A fork copies the run, its Budget included,
        as the workflow's default. A Run Change's Fork skips `pin`: the change was just checked
        against the run's own stages."""
        budget_usd = self.budget_for(source.world_id, workflow=source.budget_usd)
        now = _now()
        return Run(
            new_id("run"), source.world_id, source.workflow_id, source.workflow_version,
            source.name, "queued", now, now, [], budget_usd=budget_usd,
        )  # fmt: skip

    def _enqueue(self, run: Run) -> None:
        """A process that does not host DBOS leaves the queued run to the API's `Runner.adopt`,
        which enqueues it on the next sweep."""

    def notify(self, event: Event) -> None:
        """Hand a recorded event to the Notifier. A notifier outage (Slack) is logged and never
        fails the caller: the run is already saved and the event recorded."""
        try:
            self.notifier.notify(event)
        except Exception:
            log.exception("notifier failed on %s for run %s", event.type, event.run_id)

    def get(self, run_id: str) -> Run | None:
        return self.storage.get_run(run_id)

    def gates(self) -> list[Gate]:
        """Each with the publish preview of the stage after it, when that stage publishes, or
        with the Budget and spend of its top run when it waits for money."""
        return [
            self._with_budget(g) if g.kind == "budget" else replace(g, publish=self._preview(g))
            for g in self.storage.open_gates()
        ]

    def _preview(self, gate: Gate) -> PublishPreview | None:
        run = self.storage.get_run(gate.run_id)  # None only if deleted since the listing
        return self.preview_after(run, gate.stage_index) if run else None

    def _with_budget(self, gate: Gate) -> Gate:
        top = self.top(self._run(gate.run_id))
        spent, limit = top.spent_usd, top.budget_usd or 0.0
        return replace(gate, ask=budget.ask(spent, limit), budget_usd=limit, spent_usd=spent)

    def preview_after(
        self, run: Run, index: int, edits: dict[str, Any] | None = None
    ) -> PublishPreview | None:
        try:
            _, after = self.storage.stage_def(run.id, index + 1)
        except KeyError:
            return None
        return self.previews(run, after, edits or {}) if after.type == "connector" else None

    def decide(
        self,
        run_id: str,
        choice: str | None = None,
        note: str = "",
        send_back_to: str | None = None,
        proposal: bool = False,
        artifact: str | None = None,
        edits: dict[str, Any] | None = None,
        seen: dict[str, Any] | None = None,
        gate: tuple[int, int] | None = None,
        by: str | None = None,
    ) -> Run:
        """Answer the gate the run waits at. Writes the decision artifact, then resumes the run.
        A skill gate takes only the answer of its proposal (`proposal=True`, from Learning).
        `artifact` names the chosen or approved artifact by its path in the world folder; it
        must come from a stage before the gate, and a pick with no `choice` takes its title.
        Before a publish stage, `edits` replace the values of the fields its connector marks
        editable, and every field's value is recorded with the decision. `seen` holds the values
        the gate showed; a field that is not editable and has changed since (a relative schedule
        resolving to another day) is refused, so what posts is what was confirmed.
        A Remote Approval names the gate it answers as `gate`, (stage index, visit), so a stale
        message never answers a later gate or a later visit to the same one, and `by`, who
        answered, which the decision records.

        Raises KeyError for an unknown run, Illegal when the run is not at a gate, when the
        send-back target is not an earlier stage, on a send-back past the gate's limit, when someone
        else already answered this gate, or when the publish stage after it has problems
        (send back or abort instead), and ValueError when a pick gate gets no choice or
        the artifact is not one of this run's before the gate, or when an edit is refused (a field
        that is not editable, a value the connector reports a problem with, or the next stage does
        not publish), or a field the gate showed has changed.
        """
        try:
            run = self._run(run_id)
        except Illegal:
            if gate is None:
                raise
            raise Illegal("this gate was already answered") from None  # the run has ended
        index = next((s.index for s in run.stages if s.status == "waiting"), None)
        if gate is not None and (index, run.send_backs) != gate:
            raise Illegal("this gate was already answered")
        if run.over_budget:
            raise Illegal("the run waits at a budget gate: add money or abort")
        if index is None:
            raise Illegal("the run is not waiting at a gate")
        _, stage = self.storage.stage_def(run_id, index)
        kind = stage.config.get("kind") or "approve"
        if kind == "skill" and not proposal:
            raise Illegal("a skill gate is answered by deciding its proposal")
        note = note.strip()
        if artifact:
            file = self._artifact_before(run, index, artifact)
            choice = (choice or "").strip() or artifact_title(file)
        if kind == "pick" and not (choice or "").strip():
            raise ValueError("a pick gate needs a choice")
        if send_back_to is not None:
            if kind != "approve":
                raise Illegal(f"a {kind} gate cannot send back")
            targets = [s.index for s in run.stages if s.name == send_back_to and s.index < index]
            if not targets:
                raise Illegal(f"no earlier stage named {send_back_to!r}")
            limit = int(stage.config.get("max_send_backs") or MAX_SEND_BACKS)
            if sum(1 for d in run.decisions if d.stage_index == index and d.send_back_to) >= limit:
                times = "once" if limit == 1 else f"{limit} times"
                raise Illegal(f"already sent back {times}; approve or abort")
            event: RunEvent = SentBack(index, targets[0])
        else:
            event = GateAnswered(index)
        transition(kinds_of(run), state_of(run), event)  # raises Illegal before anything is written
        edits = edits or {}
        shown = None if send_back_to else self.preview_after(run, index)
        if edits and shown is None:
            raise ValueError("only a gate before a publish stage takes edits")
        if shown and edits:
            editable = {f.key for f in shown.fields if f.editable}
            fixed = sorted(k for k in edits if k not in editable)
            if fixed:
                raise ValueError(f"{', '.join(fixed)} cannot be edited at the gate")
            shown = self.preview_after(run, index, edits)
        if shown and shown.problems:
            # Going on would only fail at publish: an edit is bad input, else the stage is broken.
            raise (ValueError if edits else Illegal)("; ".join(shown.problems))
        for f in shown.fields if shown else []:
            if not f.editable and seen and f.key in seen and seen[f.key] != f.value:
                raise ValueError(
                    f"{f.label} changed since the gate was shown, to {f.value or 'nothing'}:"
                    " check it and confirm again"
                )
        decision = Decision(
            new_id("dec"),
            index,
            stage.name,
            kind,
            choice,
            note,
            send_back_to,
            "",
            _now(),
            artifact or None,
            shown.values() if shown else None,
            by,
        )
        decision = self._write_decision(run, decision)
        message: dict[str, Any] = {"action": "decide", "index": index}
        if isinstance(event, SentBack):
            message["to"] = event.to
        try:
            self._send(run_id, message)
        except Exception:
            # Undo the answer so the gate can be answered again once DBOS is reachable.
            self.storage.remove_decision(decision.id)
            Path(decision.path).unlink(missing_ok=True)
            raise
        return self._run(run_id)

    def propose(self, run_id: str, change: RunChange) -> ChangeProposal:
        """What `change` would do to the run, writing nothing: the stage's config before and
        after (a Rerun's or Skip's stages by name with their status, a Restructure's stage
        lists), and the violations that would refuse it, the run's state (a stage that has
        started, a run at work) and the workflow check of the run's stages with the change in
        them; a Rerun's cost too. A Restructure, and any change to a run that has ended, would
        continue as a Fork from `fork_from`. Raises KeyError for an unknown run and ValueError
        for a stage not in the run or a kind of change that cannot be applied yet."""
        run = self._any(run_id)
        if change.kind == "artifact_edit":
            return self._artifact_edit(run, change)[1]
        if change.kind in STEPS:
            return self._propose_step(run, change)
        stage, stages = edited(run, change)
        violations, checked = self._checked(run, change, stage, stages)
        problem, index = self._refusal(run, change, stage, stages)
        before, after = shown(run, change, stage, checked)
        return ChangeProposal(
            change, stage.name, before, after, [p for p in [problem] if p] + violations,
            run.revision, index is None,
            fork_from=None if index is None else checked[index].name,
        )  # fmt: skip

    def apply(self, run_id: str, change: RunChange, revision: int, by: str) -> AppliedChange:
        """Apply `change` as `by` (the operator, a token's name, a Remote Approval's person),
        checked again: write the stage's new config, record the change and emit `run.changed`.
        A Restructure, or any change to a run that has ended, starts a Fork with the change in
        it instead, which the change records as `fork`; a live run then ends aborted.
        `revision` is the proposal's; raises Illegal, naming what moved, when the run moved on
        since, or when the stage has started, ValueError with the check's violations, and as
        `propose` does otherwise."""
        run = self._any(run_id)
        if run.revision != revision:
            raise Illegal(moved(run, revision))
        if change.kind == "artifact_edit":
            return self._apply_artifact_edit(run, change, by)
        if change.kind in STEPS:
            return self._apply_step(run, change, by)
        stage, stages = edited(run, change)
        violations, checked = self._checked(run, change, stage, stages)
        problem, index = self._refusal(run, change, stage, stages)
        if problem:
            raise Illegal(problem)
        if violations:
            raise ValueError("; ".join(violations))
        before, after = shown(run, change, stage, checked)
        applied = AppliedChange(
            new_id("change"), run.id, change.kind, stage.id, stage.index, stage.name,
            after, before, by, _now(), revision + 1,
        )  # fmt: skip
        if index is not None:
            return self._fork_with(run, index, checked, applied)
        if not self.storage.add_change(applied, revision):
            raise Illegal(moved(self._run(run_id), revision))  # it moved while being checked
        return applied

    def _artifact_edit(self, run: Run, change: RunChange) -> tuple[Path, ChangeProposal]:
        """The file an Artifact Edit rewrites and its proposal: the text before and after, the
        violations (a stage that has not finished, a later stage running that reads the file or
        one that made a newer file of its name, the text check) and the finished stages after
        it that already read the file: a swarm stage is shown every earlier artifact, a
        connector reads its inputs. On a run that has ended it continues as a Fork from the
        stage after, whose stages all read the new text. Raises ValueError for a file the stage
        did not make or one that is not text."""
        stage = next((s for s in run.stages if s.id == change.stage), None)
        if stage is None:
            raise ValueError(f"no stage {change.stage} in this run")
        name, text = str(change.payload.get("path") or ""), change.payload.get("text")
        file = stage_folder(self.data_dir, run, stage.index) / "artifacts" / name
        if Path(name).name != name or name.startswith(".") or not file.is_file():
            raise ValueError(f"{stage.name} made no file {name}")
        if not isinstance(text, str):
            raise ValueError("an artifact_edit needs the file's new text")
        try:
            old = file.read_text() if file.suffix.lower() in TEXT else None
        except UnicodeDecodeError:
            old = None
        if old is None:
            raise ValueError(
                f"{name} is not a text file (markdown, JSON, YAML or plain text): a Rerun"
                " remakes it"
            )
        later = run.stages[stage.index + 1 :]
        reads = [s for s in later if s.type == "swarm" or name in s.inputs]
        problems = [p for p in [not_finished(stage)] if p]
        problems += [
            f"{s.name} is running and reads {name}: pause first or add a task"
            for s in reads
            if s.status == "running"
        ]
        problems += [
            f"{s.name} made a newer {name}; edit that one"
            for s in later
            if (stage_folder(self.data_dir, run, s.index) / "artifacts" / name).is_file()
        ]
        problems += self.check_text(run, name, old, text)
        before, after = {"path": name, "text": old}, {"path": name, "text": text}
        if run.status in TERMINAL:
            fork = stage.index + 1
            problems += [p for p in [unforkable(run, fork)] if p]
            return file, ChangeProposal(
                change, stage.name, before, after, problems, run.revision, False,
                fork_from=run.stages[fork].name if fork < len(run.stages) else None,
            )  # fmt: skip
        read_by = [s.name for s in reads if s.status == "done"]
        return file, ChangeProposal(
            change, stage.name, before, after, problems, run.revision, read_by=read_by
        )

    def _apply_artifact_edit(self, run: Run, change: RunChange, by: str) -> AppliedChange:
        """Keep the file's text as it was in the stage's `previous/` folder, numbered, write the
        new text, record the change and emit `artifact.created` so the Library and Timeline
        reload. A run that moved meanwhile gets the file back as it was. A run that has ended
        keeps its file: the Fork gets the new text."""
        file, proposal = self._artifact_edit(run, change)
        stage = next(s for s in run.stages if s.id == change.stage)
        ended = run.status in TERMINAL
        if problem := not_finished(stage) or (ended and unforkable(run, stage.index + 1)):
            raise Illegal(problem)
        if proposal.violations:
            raise ValueError("; ".join(proposal.violations))
        if not proposal.in_place:
            applied = AppliedChange(
                new_id("change"), run.id, change.kind, stage.id, stage.index, stage.name,
                proposal.after, proposal.before, by, _now(), run.revision + 1,
            )  # fmt: skip
            return self._fork_with(run, stage.index + 1, defs(run), applied)
        world = self.data_dir / "worlds" / run.world_id
        previous = file.parent.parent / "previous"
        previous.mkdir(exist_ok=True)
        kept = previous / f"{len(list(previous.glob(f'*-{file.name}'))) + 1}-{file.name}"
        applied = AppliedChange(
            new_id("change"), run.id, change.kind, stage.id, stage.index, stage.name,
            proposal.after, {**proposal.before, "kept": str(kept.relative_to(world))}, by,
            _now(), run.revision + 1,
        )  # fmt: skip
        try:
            kept.write_text(proposal.before["text"])
            file.write_text(proposal.after["text"])
            if not self.storage.add_change(applied, run.revision):
                raise Illegal(moved(self._run(run.id), run.revision))
        except BaseException:  # never leave the file changed with no change recorded
            file.write_text(proposal.before["text"])
            kept.unlink(missing_ok=True)
            raise
        self.storage.record(
            Event.new(
                "artifact.created",
                world_id=run.world_id,
                run_id=run.id,
                payload={
                    "type": artifact_type(proposal.after["text"]),
                    "path": str(file.relative_to(world)),
                    "author": by,
                    "stage_id": stage.id,
                },
            )
        )
        return applied

    def _step(
        self, run: Run, change: RunChange
    ) -> tuple[RunStageView, RunEvent, dict[str, Any], str | None, list[str], int | None]:
        """A Rerun or a Skip: its stage, the event the run's workflow takes for it, the stages
        it changes by name with their status now, why the run cannot take it now, the workflow
        check's violations the change itself brings (a Skip's missing input or ungated
        publish, ADR 0007), and on a run that has ended the stage its Fork runs anew from (the
        Rerun's start, the skipped stage), else None. A stage that failed re-runs too: in place
        while the run is live, as a failed run is until its retro settles (waiting at its gate,
        or paused), the stages the failure passed over running again; as a Fork once the run
        has ended. A run that has ended can skip a stage that failed too."""
        stage = target(run, change)
        ended = run.status in TERMINAL
        violations: list[str] = []
        if change.kind == "rerun":
            keep = tuple(c.stage_index for c in run.changes if c.kind == "skip")
            event: RunEvent = Rerun(stage.index, keep=keep)
            start = rerun_from(kinds_of(run), [s.status for s in run.stages], stage.index)
            again = rerun_again([s.status for s in run.stages], start, keep)
            resets = [s for s, a in zip(run.stages[start:], again, strict=True) if s.status != a]
            before = {s.name: s.status for s in resets}
            not_now = None
            if stage.status not in ("done", "failed"):
                not_now = f"{stage.name} has not {'run' if ended else 'finished'}"
        else:
            event, start = Skip(stage.index), stage.index
            before = {stage.name: stage.status}
            if ended:
                fine = stage.status in ("pending", "failed")
                not_now = None if fine else f"{stage.name} is {stage.status}"
            else:
                not_now = None if stage.status == "pending" else f"{stage.name} has started"
            kept = [s for s in run.stages if s.status != "skipped"]  # no longer gate or make
            every = [StageDef(s.name, s.type, s.config) for s in kept]
            known = set(self.check(run, every)[0])
            without = [d for d, s in zip(every, kept, strict=True) if s is not stage]
            violations = [v for v in self.check(run, without)[0] if v not in known]
        if ended:
            return stage, event, before, not_now or unforkable(run, start), violations, start
        if not_now is None:
            try:
                transition(kinds_of(run), state_of(run), event)
            except Illegal as exc:
                not_now = str(exc)
        return stage, event, before, not_now, violations, None

    def _propose_step(self, run: Run, change: RunChange) -> ChangeProposal:
        stage, _, before, not_now, violations, fork = self._step(run, change)
        after = dict.fromkeys(before, "pending" if change.kind == "rerun" else "skipped")
        problems = ([not_now] if not_now else []) + violations
        top = self.top(run)
        left = None if top.budget_usd is None else max(0.0, top.budget_usd - top.spent_usd)
        if fork is not None:  # the Fork starts afresh, on the Budget `_new_fork` gives it
            try:
                left = self.budget_for(run.world_id, workflow=run.budget_usd)
            except MonthSpent as exc:
                left = 0.0
                problems.append(str(exc))
        cost = None
        if change.kind == "rerun":
            spent = self.stage_spend(run.id)
            reset = [s for s in run.stages if s.name in before]
            cost = sum(spent.get(s.id, 0.0) / visits(run, s) for s in reset)
        return ChangeProposal(
            change, stage.name, before, after, problems, run.revision, fork is None,
            cost_usd=cost, budget_left_usd=left,
            fork_from=None if fork is None else run.stages[fork].name,
        )  # fmt: skip

    def _apply_step(self, run: Run, change: RunChange, by: str) -> AppliedChange:
        """Record the Rerun or Skip, then tell the run's workflow, which resets or passes over
        the stages once it is free (a Skip while a stage runs lands when it ends). On a run that
        has ended it starts the Fork instead."""
        stage, event, before, not_now, violations, fork = self._step(run, change)
        if not_now:
            raise Illegal(not_now)
        if violations:
            raise ValueError("; ".join(violations))
        payload = {"note": str(change.payload.get("note") or "").strip()}
        applied = AppliedChange(
            new_id("change"), run.id, change.kind, stage.id, stage.index, stage.name,
            payload if change.kind == "rerun" else {}, before, by, _now(), run.revision + 1,
        )  # fmt: skip
        if fork is not None:
            return self._fork_with(run, fork, defs(run), applied)
        if not self.storage.add_change(applied, run.revision):
            raise Illegal(moved(self._run(run.id), run.revision))
        # ponytail: a DBOS outage here leaves the change recorded but not taken; the run page
        # still shows the stage as it was, so apply it again once DBOS is reachable.
        if isinstance(event, Rerun):
            message = {"action": "rerun", "to": event.to, "change": applied.id}
            self._send(run.id, {**message, "keep": list(event.keep)})
        else:
            self._send(run.id, {"action": "skip", "index": stage.index, "change": applied.id})
        return applied

    def _checked(
        self, run: Run, change: RunChange, stage: RunStageView, stages: list[StageDef]
    ) -> tuple[list[str], list[StageDef]]:
        """The workflow check of `stages`, the run's with `change` in, and `stages` as it parsed
        them. The run's skipped stages, but for the target, are left out of the check: they no
        longer gate or make anything. A Restructure's stages from the target on are all new."""
        mine = len(stages) if change.kind == "stage_edit" else stage.index
        kept = [
            i
            for i in range(len(stages))
            if i >= mine or i == stage.index or run.stages[i].status != "skipped"
        ]
        violations, parsed = self.check(run, [stages[i] for i in kept])
        checked = list(stages)
        for i, d in zip(kept, parsed, strict=True):
            checked[i] = d
        return violations, checked

    def _refusal(
        self, run: Run, change: RunChange, stage: RunStageView, stages: list[StageDef]
    ) -> tuple[str | None, int | None]:
        """Why the run's state refuses the change, and the index a Fork would run anew from, None
        when it applies in place. `stages` are the run's with the change in, as given, so the
        stages before it compare equal. A change never touches the running stage, and a Fork
        never starts at a publish: it opens the gate before it again (ADR 0007)."""
        if change.kind != "restructure" and run.status not in TERMINAL:
            return self._not_editable(run, stage), None
        old = [StageDef(s.name, s.type, s.config) for s in run.stages]
        pairs = enumerate(zip(old, stages, strict=False))
        first = next((i for i, (a, b) in pairs if a != b), min(len(old), len(stages)))
        unfinished = next((s for s in run.stages if s.status not in ("done", "skipped")), None)
        index = min(first, len(stages) if unfinished is None else unfinished.index)
        if old == stages:
            return "nothing would change", index
        if index >= len(stages):
            return "the Fork would have nothing to run", index
        if index and (shown := self.previews(run, stages[index], {})) and not shown.private:
            index -= 1  # the gate the publish follows asks a person again, with a fresh preview
        if run.status in TERMINAL:
            return None, index
        if unfinished is not None and unfinished.status == "running":
            return f"{unfinished.name} is running: pause first or add a task", index
        started = [s.name for s in run.stages[first:] if s.status not in ("pending", "skipped")]
        if started:
            return f"{started[0]} has started; a Restructure changes only the stages to come", index
        return None, index

    def _fork_with(
        self, source: Run, index: int, stages: list[StageDef], change: AppliedChange
    ) -> AppliedChange:
        """Start the Fork a change continues in, from stage `index` with `stages`, record the
        change on `source` with a link to it, and end `source` if it is still live."""
        fork = self._new_fork(source)
        change = replace(change, fork=RunLink(fork.id, fork.world_id, fork.name))
        saved = self.storage.fork_change(fork, source, index, stages, change)
        if saved is None:
            raise Illegal(moved(self._any(source.id), change.revision - 1))
        copy_run_files(self.data_dir, source, saved, index)
        if change.kind == "artifact_edit":  # the Fork's copy of the file gets the new text
            path = stage_folder(self.data_dir, saved, change.stage_index) / "artifacts"
            (path / change.payload["path"]).write_text(change.payload["text"])
        self._enqueue(saved)
        if source.status not in TERMINAL:
            with suppress(Illegal):  # it ended meanwhile; the change and its Fork still stand
                self.abort(source.id)
        return change

    def _not_editable(self, run: Run, stage: RunStageView) -> str | None:
        """Why the stage cannot be edited now: it has started, or it is the publish the waiting
        gate is confirming, whose preview was sent when the gate opened (ADR 0007)."""
        if problem := not_pending(stage):
            return problem
        gate = run.stages[stage.index - 1] if stage.index else None
        if gate and gate.status == "waiting" and self.preview_after(run, gate.index):
            return f"{gate.name} is confirming {stage.name}: answer or send back the gate first"
        return None

    def pause(self, run_id: str) -> Run:
        """Pause takes effect at once at a gate, otherwise at the running stage's next agent
        step (`check_stop`); the stage runs again, its agents resuming, on resume."""
        run = self._control(run_id, Paused(), "pause")
        self.storage.set_stop(run_id, "pause")  # after the send: a stop with no pause would loop
        return run

    def resume(self, run_id: str) -> Run:
        run = self._run(run_id)
        transition(kinds_of(run), state_of(run), Resumed())  # raises Illegal when it cannot apply
        # Only a legal resume lifts a pause, and never an abort: an illegal one mid-stage would
        # let the agents go on. Before the send, so the rerun stage does not stop at once.
        if self.storage.stop_of(run_id) == "pause":
            self.storage.set_stop(run_id, None)
        self._send(run_id, {"action": "resume"})
        return run

    def abort(self, run_id: str) -> Run:
        """Abort the run and every child run it started (ADR 0013).

        The run hears its abort before its child's end, so it ends aborted rather than failed.
        """
        try:
            run = self._run(run_id)
        except Illegal:
            self._abort_children(run_id)  # a finished run's live descendants still stop
            raise
        state, _ = transition(kinds_of(run), state_of(run), Aborted())
        if run.status == "queued":
            self.client.cancel_workflow(run_id)
            ended = self.storage.save_state(run_id, state)
            if ended is None:
                raise KeyError(f"run not found: {run_id}")
            event = ended_event(ended)
            self.storage.record(event)
            self.notify(event)
            if ended.parent_run_id is not None:  # no workflow ran to tell the parent
                message = {"action": "child", "child": run_id, "status": "aborted"}
                self._send(ended.parent_run_id, message)
            run = ended
        else:
            self._send(run_id, {"action": "abort"})
            self.storage.set_stop(run_id, "abort")
        self._abort_children(run_id)
        return run

    def delete_world(self, world_id: str) -> None:
        """Cancel the world's unfinished runs, then drop its rows and its queue. Through the DBOS
        client, so it reaches runs hosted by another process too (the MCP server deleting a
        world)."""
        for run_id in self.storage.active_run_ids(world_id):
            self.client.cancel_workflow(run_id)
            self._send(run_id, WAKE)  # a run blocked waiting for a message sees the cancel
        self.storage.delete_world(world_id)
        # A queue left behind keeps a DBOS thread polling it, a write transaction every poll,
        # for as long as the app runs; deleting its row stops that thread.
        try:
            self.client.delete_queue(queue_name(world_id))
        except OperationalError as exc:
            if "no such table" not in str(exc):  # DBOS never launched here, so no queue either
                raise

    def _abort_children(self, run_id: str) -> None:
        for child in self.storage.child_run_ids(run_id):
            try:
                self.abort(child)
            except Illegal:
                pass  # the child already ended; abort still walked its children

    def family(self, run_id: str) -> list[str]:
        """The run and every run below it: the runs whose spend counts against its Budget, since
        a child run spends its parent's (ADR 0013)."""
        return [run_id, *(r for c in self.storage.child_run_ids(run_id) for r in self.family(c))]

    def top(self, run: Run) -> Run:
        """The run at the top of `run`'s family, which holds the Budget (ADR 0013)."""
        while run.parent_run_id is not None and (up := self.storage.get_run(run.parent_run_id)):
            run = up
        return run

    def check_stop(self, run_id: str) -> str | None:
        """The cost hook calls this after every estimate. Refreshes the spend of the run and of
        each run above it, warns once at 80 percent of the Budget (`budget.warning`) and returns
        why every agent must stop: the run was paused or aborted, or the Budget is spent. None
        while they may go on."""
        reason = self._over_budget(run_id)
        match self.storage.stop_of(run_id):
            case "pause":
                return "The run was paused. It resumes when the run is resumed."
            case "abort":
                return "The run was aborted."
        return reason

    def _over_budget(self, run_id: str) -> str | None:
        run, top = self.storage.get_run(run_id), None
        while run is not None:
            top = self.storage.set_spent(run.id, self.spend(self.family(run.id))) or run
            run = self.storage.get_run(run.parent_run_id) if run.parent_run_id else None
        if top is None or top.budget_usd is None:
            return None
        level = budget.level(top.spent_usd, top.budget_usd)
        if level == "ok":
            return None
        if self.storage.mark_warned(top.id, top.budget_usd):
            payload = {
                "run_id": top.id,
                "name": top.name,
                "spent_usd": top.spent_usd,
                "budget_usd": top.budget_usd,
            }
            event = Event.new(
                "budget.warning", world_id=top.world_id, run_id=top.id, payload=payload
            )
            self.storage.record(event)
            self.notify(event)  # a notifier outage must not stop the agents
        if level == "spent":
            spent = f"The run's budget of ${top.budget_usd:.2f} is spent."
            return f"{spent} It resumes once money is added."
        return None

    def set_budget(self, run_id: str, usd: float) -> Run:
        """Set the Budget of the run's family (held by its top run) to `usd`. Every run of the
        family waiting at a budget gate goes on. Raises ValueError unless `usd` is above what
        the family has spent, so setting the same total twice adds nothing twice."""
        run = self._run(run_id)
        top = self.top(run)
        if usd <= top.spent_usd:
            raise ValueError(f"the run has spent ${top.spent_usd:.2f}; set a budget above that")
        self.storage.set_budget(top.id, usd)
        for member in self.family(top.id):
            waiting = self.storage.get_run(member)
            if waiting is not None and waiting.over_budget and waiting.status not in TERMINAL:
                self._send(member, {"action": "budget"})
        return self._run(run_id)

    def baseline(self, run_id: str) -> Run | None:
        """The newest finished baseline run of the run's workflow, to measure it against."""
        run = self.storage.get_run(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        return self.storage.latest_baseline(run.workflow_id)

    @property
    def client(self) -> DBOSClient:
        if self._client is None:
            self._client = DBOSClient(
                system_database_url=f"sqlite:///{self.system_db}",
                application_name=APP_NAME,
                lazy=True,
            )
        return self._client

    def schedule(self, workflow_id: str, cron: str | None) -> None:
        """Make DBOS start a run of the workflow's current version on `cron` (UTC; six fields
        put seconds first), replacing any earlier schedule; None removes it. Takes effect
        within the API's scheduler poll. Raises ValueError for a cron DBOS refuses."""
        name = SCHEDULE_PREFIX + workflow_id
        try:
            self.client.delete_schedule(name)
            if cron is not None:
                self.client.create_schedule(
                    schedule_name=name,
                    workflow_name=SCHEDULED_WORKFLOW,
                    schedule=cron,
                    context={"workflow_id": workflow_id},
                    application_name=APP_NAME,
                )
        except OperationalError as exc:
            # DBOS has never launched here, so its tables are not there yet (a save from the
            # MCP process before the API's first start). `sync_schedules` at launch catches up.
            # Anything else (a busy database) raises: the cron would otherwise silently wait.
            if "no such table" not in str(exc):
                raise
            log.info("DBOS is not launched; %s is scheduled at the next launch", workflow_id)
        except Exception as exc:  # DBOSException: an invalid cron is the only cause left
            raise ValueError(str(exc)) from exc

    def schedules(self) -> dict[str, str]:
        """Every workflow DBOS has a schedule for, to its cron expression."""
        found = self.client.list_schedules(schedule_name_prefix=SCHEDULE_PREFIX)
        return {s["schedule_name"][len(SCHEDULE_PREFIX) :]: s["schedule"] for s in found}

    def sync_schedules(self, wanted: dict[str, str]) -> None:
        """Make DBOS's schedules exactly `wanted` (workflow id to cron): the workflows table is
        the source of truth, so a schedule edited while the API was down, or a workflow deleted
        with its world, is reconciled at launch."""
        current = self.schedules()
        for workflow_id in set(current) | set(wanted):
            if current.get(workflow_id) != wanted.get(workflow_id):
                self.schedule(workflow_id, wanted.get(workflow_id))

    def close(self) -> None:
        if self._client is not None:
            self._client.destroy()
            self._client = None

    def _control(self, run_id: str, event: RunEvent, action: str) -> Run:
        run = self._run(run_id)
        transition(kinds_of(run), state_of(run), event)  # raises Illegal when it cannot apply
        self._send(run_id, {"action": action})
        return run

    def _run(self, run_id: str) -> Run:
        return live(self._any(run_id))

    def _any(self, run_id: str) -> Run:
        """The run, live or ended; KeyError when there is none."""
        run = self.storage.get_run(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        return run

    def _send(self, run_id: str, message: dict[str, Any]) -> None:
        self.client.send(run_id, message, topic=CONTROL_TOPIC)

    def _artifact_before(self, run: Run, index: int, path: str) -> Path:
        """The file at `path` (relative to the world folder), which must be an artifact of a
        stage of this run before stage `index`."""
        folder = run_folder(self.data_dir, run).resolve()
        file = (folder.parent.parent / path).resolve()
        parts = file.relative_to(folder).parts if file.is_relative_to(folder) else ()
        earlier = {s.id for s in run.stages if s.index < index}
        if not (len(parts) >= 3 and parts[0] in earlier and file.is_file()):
            raise ValueError(f"{path} is not an artifact of this run before this gate")
        return file

    def _write_decision(self, run: Run, decision: Decision) -> Decision:
        """Record the decision row, then its markdown file."""
        folder = stage_folder(self.data_dir, run, decision.stage_index)
        folder.mkdir(parents=True, exist_ok=True)
        attempt = sum(d.stage_index == decision.stage_index for d in run.decisions) + 1
        path = folder / ("decision.md" if attempt == 1 else f"decision-{attempt}.md")
        decision = replace(decision, path=str(path))
        # Illegal: already answered, or the run moved since `run` was read (a Rerun landed)
        self.storage.add_decision(run.id, decision, run.send_backs, run.revision)
        path.write_text(decision_markdown(run, decision))
        return decision


STEPS = ("rerun", "skip")
"""The Run Changes that move the run's stages rather than edit them, through its workflow."""


def unforkable(run: Run, index: int) -> str | None:
    """Why a Fork of the run cannot run anew from stage `index`: nothing is left to run, or a
    stage before it has not finished, as a Fork copies only finished stages."""
    if index >= len(run.stages):
        return "the Fork would have nothing to run"
    unfinished = next((s for s in run.stages[:index] if s.status not in ("done", "skipped")), None)
    return None if unfinished is None else f"{unfinished.name} has not finished"


def defs(run: Run) -> list[StageDef]:
    return [StageDef(s.name, s.type, s.config) for s in run.stages]


def target(run: Run, change: RunChange) -> RunStageView:
    stage = next((s for s in run.stages if s.id == change.stage), None)
    if stage is None:
        raise ValueError(f"no stage {change.stage} in this run")
    return stage


def visits(run: Run, stage: RunStageView) -> int:
    """How often the stage has run: once, plus each send-back and Rerun that reset it after it
    ran, so its cost so far over this is its average cost."""
    index = {s.name: s.index for s in run.stages}
    back = sum(
        1
        for d in run.decisions
        if d.send_back_to
        and index.get(d.send_back_to, stage.index + 1) <= stage.index < d.stage_index
    )
    return 1 + back + sum(1 for c in run.changes if c.kind == "rerun" and stage.name in c.before)


def edited(run: Run, change: RunChange) -> tuple[RunStageView, list[StageDef]]:
    """The stage a Run Change targets and the run's stages with the change in them. A
    Restructure's stages replace the run's from the target on; the run's Retro stays last."""
    if change.kind not in ("stage_edit", "restructure"):
        raise ValueError(f"a {change.kind} cannot be applied yet")
    stage = target(run, change)
    defs = [StageDef(s.name, s.type, s.config) for s in run.stages]
    if change.kind == "stage_edit":
        return stage, [
            replace(d, config=change.payload) if d is defs[stage.index] else d for d in defs
        ]
    new = change.payload.get("stages")
    if not isinstance(new, list) or not all(
        isinstance(d, dict) and d.get("name") and d.get("type") for d in new
    ):
        raise ValueError("a Restructure's stages each need a name and a type")
    tail = [
        StageDef(d["name"], d["type"], {k: v for k, v in d.items() if k not in ("name", "type")})
        for d in new
    ]
    if not tail:
        raise ValueError("a Restructure needs at least one stage")
    retro = [d for d in defs[stage.index :] if d.type == "retro"]
    return stage, [*defs[: stage.index], *tail, *retro]


def shown(
    run: Run, change: RunChange, stage: RunStageView, stages: list[StageDef]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A change's before and after: the stage's config, or for a Restructure the stages from
    the target on (the Retro left out) in the workflow's stage shape."""
    if change.kind != "restructure":
        return stage.config, stages[stage.index].config

    def listed(defs: list[RunStageView] | list[StageDef]) -> dict[str, Any]:
        return {
            "stages": [
                {"name": d.name, "type": d.type, **d.config} for d in defs if d.type != "retro"
            ]
        }

    return listed(run.stages[stage.index :]), listed(stages[stage.index :])


def live(run: Run) -> Run:
    """The run, while it has not ended; Illegal once it has."""
    if run.status in TERMINAL:
        raise Illegal(f"run is {run.status}")
    return run


def not_pending(stage: RunStageView) -> str | None:
    """Why the stage can no longer be edited, None while it has not started (ADR 0027)."""
    if stage.status == "running":
        return f"{stage.name} is running: pause first or add a task"
    if stage.status != "pending":
        return f"{stage.name} is {stage.status}; only a stage that has not started can be edited"
    return None


def not_finished(stage: RunStageView) -> str | None:
    """Why the files of the stage cannot be edited, None once it has finished (ADR 0027)."""
    if stage.status != "done":
        return f"{stage.name} is {stage.status}; only a file a finished stage made can be edited"
    return None


def artifact_type(text: str) -> str:
    """The `type` in a markdown artifact's frontmatter, else `file`, as Memory reads it."""
    if text.startswith("---\n"):
        for line in text[4:].split("\n---\n", 1)[0].splitlines():
            if line.startswith("type: "):
                try:
                    return str(json.loads(line[6:]))
                except ValueError:
                    return line[6:].strip().strip("'")
    return "file"


def moved(run: Run, revision: int) -> str:
    """What happened to the run since `revision`: the latest change applied since, else where
    the run is now."""
    since = [c for c in run.changes if c.revision > revision]
    live = next((s for s in run.stages if s.status in ("running", "waiting")), None)
    if since:
        done = {"rerun": "re-run", "skip": "skipped"}.get(since[-1].kind, "edited")
        what = f"{since[-1].stage_name} was {done} by {since[-1].by}"
    else:
        what = f"it is now at {live.name}" if live else f"it is now {run.status}"
    return f"The run moved on since this was proposed: {what}."


def artifact_title(file: Path) -> str:
    """The `title` in a markdown artifact's frontmatter (or its sidecar's), else the name of
    the file, or of the binary a sidecar carries. Frontmatter values are JSON (Memory writes
    them so), read here without importing Memory."""
    sidecar = file.with_name(f"{file.name}.md")
    if file.suffix != ".md" and sidecar.is_file():
        file = sidecar
    for line in file.read_text(errors="replace").split("\n---\n", 1)[0].splitlines():
        if line.startswith("title: "):
            try:
                return str(json.loads(line[7:]))
            except ValueError:
                return line[7:]
    carried = file.suffix == ".md" and file.with_name(file.name[:-3]).is_file()
    return file.name[:-3] if carried else file.name


def decision_markdown(run: Run, decision: Decision) -> str:
    """Frontmatter is the artifact schema; the outcome is repeated in the body because the graph
    hashes bodies only (V1 spec, Memory context)."""
    front = {
        "type": "decision",
        "world": run.world_id,
        "run": run.id,
        "stage": decision.stage_name,
        "stage_index": decision.stage_index,
        "kind": decision.kind,
        "choice": decision.choice,
        "artifact": decision.artifact,
        "send_back_to": decision.send_back_to,
        "publish": decision.publish,
        "by": decision.by,
        "created": decision.decided_at,
    }
    lines = [f"{k}: {json.dumps(v)}" for k, v in front.items() if v is not None]
    if decision.send_back_to:
        outcome = f"Sent back to {decision.send_back_to}."
    elif decision.kind == "pick":
        outcome = f"Picked {decision.choice}."
    elif decision.kind == "confirm":
        outcome = "Confirmed."
    elif decision.kind == "skill":
        outcome = f"Skill proposal {decision.choice or 'approved'}."
    else:
        outcome = "Approved."
    if decision.by:
        outcome = f"{outcome[:-1]}, by {decision.by}."
    body = [f"# Decision at {decision.stage_name}", "", outcome]
    if decision.note:
        body += ["", f"Note: {decision.note}"]
    return "---\n" + "\n".join(lines) + "\n---\n\n" + "\n".join(body) + "\n"
