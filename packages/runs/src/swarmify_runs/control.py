"""Acting on a run from outside its workflow: decide at a gate, pause, resume, abort.

Any process may act. Messages reach the run's DBOS workflow, the single writer of run state,
through a DBOSClient on the shared system database, so the MCP server works without launching
DBOS. A queued run has no workflow thread yet, so abort cancels its enqueued workflow and
writes the terminal state here. The decision artifact is written here too, before the message
is sent, so the record exists even if the run has moved on by the time the message lands.
"""

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dbos import DBOSClient

from swarmify_kernel import Event, new_id
from swarmify_runs.machine import (
    TERMINAL,
    Aborted,
    GateAnswered,
    Illegal,
    Paused,
    Resumed,
    RunEvent,
    RunState,
    SentBack,
    transition,
)
from swarmify_runs.model import Decision, Gate, PublishPreview, Run, StageDef
from swarmify_runs.ports import Notifier, Storage

CONTROL_TOPIC = "control"

Previews = Callable[[str, StageDef], PublishPreview | None]
"""What a stage of a world will publish, or None when it does not publish. Runs cannot tell a
publish connector from another; the Connectors registry can, through the composition root."""


def no_previews(_world_id: str, _stage: StageDef) -> PublishPreview | None:
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def run_folder(data_dir: Path, run: Run) -> Path:
    return data_dir / "worlds" / run.world_id / "runs" / run.id


def stage_folder(data_dir: Path, run: Run, index: int) -> Path:
    """`<data>/worlds/<world>/runs/<run>/<nn>-<stage>`, readable in Obsidian (ADR 0001)."""
    name = re.sub(r"[^a-z0-9]+", "-", run.stages[index].name.lower()).strip("-") or "stage"
    return run_folder(data_dir, run) / f"{index + 1:02d}-{name}"


def copy_run_files(data_dir: Path, source: Run, run: Run, index: int) -> None:
    """Copy the source run's folder into its fork's, leaving out the folders of the stages at
    or past `index`. A stage has two: `<nn>-<name>` (its decisions, written here) and
    `<stage id>` (its notes and artifacts, written by Memory); the latter is renamed to the
    fork's stage id so the copied stage reads back under it (files are memory, ADR 0001)."""
    src, dst = run_folder(data_dir, source), run_folder(data_dir, run)
    if not src.is_dir():
        return
    renamed = {s.id: run.stages[s.index].id for s in source.stages}
    later = range(index, len(source.stages))
    skip = {source.stages[i].id for i in later}
    skip |= {stage_folder(data_dir, source, i).name for i in later}
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
    return RunState(run.status, tuple(s.status for s in run.stages), run.send_backs)


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
    ) -> None:
        self.storage = storage
        self.system_db = system_db
        self.data_dir = data_dir
        self.notifier = notifier
        self.previews = previews
        self._client: DBOSClient | None = None

    def get(self, run_id: str) -> Run | None:
        return self.storage.get_run(run_id)

    def gates(self) -> list[Gate]:
        """Each with the publish preview of the stage after it, when that stage publishes."""
        return [
            replace(g, publish=self.preview_after(g.run_id, g.stage_index, g.world_id))
            for g in self.storage.open_gates()
        ]

    def preview_after(self, run_id: str, index: int, world_id: str) -> PublishPreview | None:
        try:
            _, after = self.storage.stage_def(run_id, index + 1)
        except KeyError:
            return None
        return self.previews(world_id, after) if after.type == "connector" else None

    def decide(
        self,
        run_id: str,
        choice: str | None = None,
        note: str = "",
        send_back_to: str | None = None,
    ) -> Run:
        """Answer the gate the run waits at. Writes the decision artifact, then resumes the run.

        Raises KeyError for an unknown run, Illegal when the run is not at a gate, when the
        send-back target is not an earlier stage, on the third send-back, or when someone
        else already answered this gate, and ValueError when
        a pick gate gets no choice.
        """
        run = self._run(run_id)
        index = next((s.index for s in run.stages if s.status == "waiting"), None)
        if index is None:
            raise Illegal("the run is not waiting at a gate")
        _, stage = self.storage.stage_def(run_id, index)
        kind = stage.config.get("kind") or "approve"
        note = note.strip()
        if kind == "pick" and not (choice or "").strip():
            raise ValueError("a pick gate needs a choice")
        if send_back_to is not None:
            if kind != "approve":
                raise Illegal(f"a {kind} gate cannot send back")
            targets = [s.index for s in run.stages if s.name == send_back_to and s.index < index]
            if not targets:
                raise Illegal(f"no earlier stage named {send_back_to!r}")
            event: RunEvent = SentBack(index, targets[0])
        else:
            event = GateAnswered(index)
        transition(kinds_of(run), state_of(run), event)  # raises Illegal before anything is written
        decision = Decision(
            new_id("dec"), index, stage.name, kind, choice, note, send_back_to, "", _now()
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

    def pause(self, run_id: str) -> Run:
        """Pause takes effect at once at a gate, otherwise when the current stage finishes."""
        return self._control(run_id, Paused(), "pause")

    def resume(self, run_id: str) -> Run:
        return self._control(run_id, Resumed(), "resume")

    def abort(self, run_id: str) -> Run:
        """Abort the run and every child run it started (ADR 0013).

        The run hears its abort before its child's end, so it ends aborted rather than failed.
        """
        run = self._run(run_id)
        state, _ = transition(kinds_of(run), state_of(run), Aborted())
        if run.status == "queued":
            self.client.cancel_workflow(run_id)
            ended = self.storage.save_state(run_id, state)
            if ended is None:
                raise KeyError(f"run not found: {run_id}")
            event = ended_event(ended)
            self.storage.record(event)
            self.notifier.notify(event)
            if ended.parent_run_id is not None:  # no workflow ran to tell the parent
                message = {"action": "child", "child": run_id, "status": "aborted"}
                self._send(ended.parent_run_id, message)
            run = ended
        else:
            self._send(run_id, {"action": "abort"})
        for child in self.storage.child_run_ids(run_id):
            try:
                self.abort(child)
            except Illegal:
                pass  # the child already ended
        return run

    def family(self, run_id: str) -> list[str]:
        """The run and every run below it: the runs whose spend counts against its Budget, since
        a child run spends its parent's (ADR 0013)."""
        return [run_id, *(r for c in self.storage.child_run_ids(run_id) for r in self.family(c))]

    @property
    def client(self) -> DBOSClient:
        if self._client is None:
            self._client = DBOSClient(system_database_url=f"sqlite:///{self.system_db}", lazy=True)
        return self._client

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
        run = self.storage.get_run(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        if run.status in TERMINAL:
            raise Illegal(f"run is {run.status}")
        return run

    def _send(self, run_id: str, message: dict[str, Any]) -> None:
        self.client.send(run_id, message, topic=CONTROL_TOPIC)

    def _write_decision(self, run: Run, decision: Decision) -> Decision:
        """Record the decision row, then its markdown file."""
        folder = stage_folder(self.data_dir, run, decision.stage_index)
        folder.mkdir(parents=True, exist_ok=True)
        attempt = sum(d.stage_index == decision.stage_index for d in run.decisions) + 1
        path = folder / ("decision.md" if attempt == 1 else f"decision-{attempt}.md")
        decision = replace(decision, path=str(path))
        self.storage.add_decision(run.id, decision, run.send_backs)  # Illegal: already answered
        path.write_text(decision_markdown(run, decision))
        return decision


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
        "send_back_to": decision.send_back_to,
        "created": decision.decided_at,
    }
    lines = [f"{k}: {json.dumps(v)}" for k, v in front.items() if v is not None]
    if decision.send_back_to:
        outcome = f"Sent back to {decision.send_back_to}."
    elif decision.kind == "pick":
        outcome = f"Picked {decision.choice}."
    elif decision.kind == "confirm":
        outcome = "Confirmed."
    else:
        outcome = "Approved."
    body = [f"# Decision at {decision.stage_name}", "", outcome]
    if decision.note:
        body += ["", f"Note: {decision.note}"]
    return "---\n" + "\n".join(lines) + "\n---\n\n" + "\n".join(body) + "\n"
