"""Remote Approvals (spec V2): answering a gate outside the web UI, through a notify Connector
that offers the `remote_approvals` Capability.

The core owns the Capability's two tools. `post_gate(gate) -> {"message_ref": ...}` gets each
opened gate with the answers that fit in a message (`answers`, each a label and the `decide`
arguments to send back); `update_gate(message_ref, resolution)` gets how it closed, whether it was
answered there, in the web UI or elsewhere. The connector delivers an answer by calling
`decide_gate` on the MCP server at `/mcp/remote_approvals` with the token the core provisions for
its long-lived `listen` process, which the core keeps running (`Listeners`): Swarmify starts no
Service (ADR 0026), but a package's own process is the package, run like its stdio server.
"""

import json
import logging
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from queue import Queue
from typing import Any

from mcp.client.stdio import get_default_environment

from swarmify_connectors import Connector, ConnectorError, Connectors
from swarmify_kernel import Event
from swarmify_kernel.sqlite import connect
from swarmify_memory import ArtifactFile
from swarmify_runs import Gate, Notifier

log = logging.getLogger(__name__)
SCOPE = "remote_approvals"
PATH = f"/mcp/{SCOPE}"
CHOICES = 5
"""A pick gate's cards offered as buttons; the rest are chosen in the web UI."""
NOT_CHOSEN = frozenset({"verdict", "decision", "note", "file"})
"""Artifact types a pick never chooses, as the web UI's gate card leaves them out."""
SAID = {
    "approved": "Approved",
    "confirmed": "Confirmed",
    "answered": "Answered",
    "budget_added": "Budget raised",
}


def said(resolution: dict[str, Any]) -> str:
    """How the gate closed, as the posted message says it."""
    outcome = resolution["outcome"]
    if outcome == "aborted":
        return "The run was aborted."
    if outcome == "picked":
        what = f"Picked {resolution.get('choice') or 'one'}"
    elif outcome == "sent_back":
        what = f"Sent back to {resolution.get('send_back_to')}"
    else:
        what = SAID.get(outcome, "Answered")
    by = resolution.get("by")
    return f"{what} by {by}." if by else f"{what} in Swarmify."


def answers(gate: Gate, cards: list[ArtifactFile]) -> list[dict[str, Any]]:
    """What fits in buttons: approve and one send-back to the stage before, confirm, or a pick
    of the first few cards. A budget or skill gate is answered in the web UI."""
    if gate.kind == "confirm":
        return [{"label": "Confirm", "primary": True, "decide": {}}]
    if gate.kind == "approve":
        out: list[dict[str, Any]] = [{"label": "Approve", "primary": True, "decide": {}}]
        if gate.send_backs_left and gate.earlier_stages:
            back = gate.earlier_stages[-1]
            out.append({"label": f"Send back to {back}", "decide": {"send_back_to": back}})
        return out
    if gate.kind == "pick":
        shown = [c for c in cards if c.type not in NOT_CHOSEN and not c.fields.get("deleted")]
        shown.sort(key=lambda c: c.fields.get("rank") or 1e9)
        return [{"label": c.title, "decide": {"artifact": c.path}} for c in shown[:CHOICES]]
    return []


class RemoteApprovals:
    """The Notifier: an opened gate goes to each remote approver the world has set up, else to
    `fallback` (V1's webhook Channel), as does every other Notification; a closed gate updates
    the messages posted for it. Calls to connectors happen on one background thread, in order,
    so a run never waits on Slack and an update always follows its post."""

    def __init__(
        self,
        db: Path,
        registry: Connectors,
        fallback: Notifier,
        gate: Callable[[str], Gate | None],
        cards: Callable[[str, int], list[ArtifactFile]],
        base_url: str,
    ) -> None:
        """`gate` is the open gate of a run, `cards` the artifacts of a run before a stage."""
        self.db = db
        self.registry = registry
        self.fallback = fallback
        self.gate = gate
        self.cards = cards
        self.base_url = base_url.rstrip("/")
        self.jobs: Queue[Callable[[], None]] = Queue()
        threading.Thread(target=self._work, name="remote-approvals", daemon=True).start()

    def notify(self, event: Event) -> None:
        if event.type == "gate.closed":
            self.jobs.put(lambda: self._update(event))
            return
        posters = self.registry.remote_approvers(event.world_id)
        if event.type == "gate.opened" and posters:
            self.jobs.put(lambda: self._post(event, posters))
            return
        self.fallback.notify(event)

    def flush(self) -> None:
        """Wait until every post and update handed over so far is done."""
        self.jobs.join()

    def _work(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception:
                log.exception("a remote approval post or update failed")
            finally:
                self.jobs.task_done()

    def _post(self, event: Event, posters: list[Connector]) -> None:
        p = event.payload
        gate = self.gate(p["run_id"])
        if gate is None or gate.stage_index != p["stage_index"]:
            return  # answered before it could be posted
        shown = p.get("publish") or {}
        message = {
            "run_id": p["run_id"],
            "run_name": p["name"],
            "world_id": event.world_id,
            "stage_index": p["stage_index"],
            "visit": p["visit"],
            "stage": p["stage"],
            "kind": p["kind"],
            "ask": p["ask"],
            "link": f"{self.base_url}/worlds/{event.world_id}/runs/{p['run_id']}",
            "publish": [
                {"label": f["label"], "value": f["value"]} for f in shown.get("fields", [])
            ],
            "answers": answers(gate, self.cards(p["run_id"], p["stage_index"])),
        }
        for connector in posters:
            try:
                ref = self.registry.ask(connector, "post_gate", {"gate": message})["message_ref"]
            except (ConnectorError, KeyError) as exc:
                log.warning("%s did not post the gate of %s: %s", connector.name, p["run_id"], exc)
                self._fall_back(event)
                continue
            with closing(connect(self.db)) as conn:
                conn.execute(
                    "INSERT INTO remote_messages VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        p["run_id"],
                        p["stage_index"],
                        p["visit"],
                        event.world_id,
                        connector.name,
                        json.dumps(ref),
                    ),
                )

    def _fall_back(self, event: Event) -> None:
        try:
            self.fallback.notify(event)
        except Exception:
            log.exception("the fallback Channel failed too on %s", event.type)

    def _update(self, event: Event) -> None:
        p = event.payload
        key = (p["run_id"], p["stage_index"], p["visit"])
        with closing(connect(self.db)) as conn:
            rows: list[sqlite3.Row] = conn.execute(
                "SELECT world_id, package, message_ref FROM remote_messages"
                " WHERE run_id = ? AND stage_index = ? AND visit = ?",
                key,
            ).fetchall()
        resolution = {**p["resolution"], "text": said(p["resolution"])}
        for row in rows:
            connector = self.registry.find(row["world_id"], row["package"])
            try:
                if connector is None:
                    raise ConnectorError(f"{row['package']} is no longer enabled")
                ref = json.loads(row["message_ref"])
                args = {"message_ref": ref, "resolution": resolution}
                self.registry.ask(connector, "update_gate", args)
            except ConnectorError as exc:
                log.warning("%s did not update its gate message: %s", row["package"], exc)
        with closing(connect(self.db)) as conn:
            conn.execute(
                "DELETE FROM remote_messages WHERE run_id = ? AND stage_index = ? AND visit = ?",
                key,
            )


class Listeners:
    """The long-lived `listen` processes of remote approvers, kept running by `sync`: started
    with the package's Setup values, the MCP URL and a token of scope `remote_approvals` the core
    provisions for the package, restarted when one exits, stopped once no world wants it."""

    # ponytail: a process that keeps dying restarts every sync (15 s), with no backoff past that;
    # a revoked token is provisioned again only when the API restarts.
    def __init__(self, registry: Connectors, provision: Callable[[str], str], url: str) -> None:
        self.registry = registry
        self.provision = provision
        self.url = url
        self.tokens: dict[str, str] = {}
        self.running: dict[str, subprocess.Popen[bytes]] = {}

    def sync(self) -> None:
        wanted = {
            repr((c.name, c.version, sorted(env.items()))): (c, env)
            for c, env in self.registry.listeners()
        }
        for key, process in list(self.running.items()):
            if key not in wanted or process.poll() is not None:
                if process.returncode is not None:
                    log.warning("a listener exited with %s; starting it again", process.returncode)
                self._stop(self.running.pop(key))
        for key, (connector, env) in wanted.items():
            if key in self.running:
                continue
            if connector.name not in self.tokens:
                self.tokens[connector.name] = self.provision(connector.name)
            full = {
                **get_default_environment(),
                **env,
                "SWARMIFY_MCP_URL": self.url,
                "SWARMIFY_API_TOKEN": self.tokens[connector.name],
            }
            try:
                self.running[key] = subprocess.Popen(
                    connector.manifest["listen"], cwd=connector.folder, env=full
                )
            except OSError as exc:
                log.error("%s's listener did not start: %s", connector.name, exc)

    def stop(self) -> None:
        for process in self.running.values():
            self._stop(process)
        self.running.clear()

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
