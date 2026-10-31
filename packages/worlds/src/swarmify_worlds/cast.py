"""The Cast of a world: its named agents and their track records.

The names are fixed by role: `planner`, `worker-1` to `worker-<agent_count>`, `judge`, the names
the Swarm context runs sessions under. Version 1 of every agent is the built-in default (no
prompt, any tool the stage allows); each edit appends a version. Skins map the names to their
own labels (ADR 0004).

The record is a read model over the world's event stream, no new storage: `task.updated` says
who finished what, `verdict.created` whether a stage passed on its first verdict, `run.updated`
carries the send-backs, `agent.updated` the estimated cost.
"""

from collections.abc import Iterable
from dataclasses import replace

from swarmify_kernel import Event
from swarmify_worlds.model import CastEdit, CastMember, Record

PLANNER, JUDGE = "planner", "judge"
RETRO = "retro"
"""The retro agent: not in the Cast (the learning context owns its prompt); a skin dresses it."""
MAX_AGENTS = 8
MAX_PROMPT = 10_000


def worker(n: int) -> str:
    return f"worker-{n}"


def names(agent_count: int) -> list[tuple[str, str]]:
    """(name, role) of every agent of a cast with `agent_count` workers, in cast order."""
    workers = [(worker(i), "worker") for i in range(1, agent_count + 1)]
    return [(PLANNER, "planner"), *workers, (JUDGE, "judge")]


def members(
    agent_count: int, edits: Iterable[CastEdit], records: dict[str, Record] | None = None
) -> list[CastMember]:
    """The cast at its current versions: the defaults overlaid with the newest edit of each."""
    latest: dict[str, CastEdit] = {}
    for edit in edits:  # oldest first, so the last one of a name wins
        latest[edit.name] = edit
    records = records or {}
    cast = []
    for name, role in names(agent_count):
        member = CastMember(name, role, 1, "", None, None, records.get(name, Record()))
        if (edit := latest.get(name)) is not None:
            member = replace(
                member,
                version=edit.number,
                prompt=edit.prompt,
                tools=edit.tools,
                updated_at=edit.created_at,
            )
        cast.append(member)
    return cast


def records(events: Iterable[Event]) -> dict[str, Record]:
    """Fold the world's stream into one Record per agent name.

    A send-back counts against every agent that finished a task in the stage it went back to.
    """
    # ponytail: folds the whole stream on every call; fine for one operator's worlds. Keep a
    # running fold per world when streams grow past what reads in a blink.
    tasks: dict[str, set[str]] = {}  # agent -> task ids it finished
    worked: dict[str, set[str]] = {}  # agent -> stage ids it finished a task in
    first: dict[str, bool] = {}  # stage id -> did the first verdict pass
    sent_back: dict[str, int] = {}  # stage id -> send-backs to it
    cost: dict[str, float] = {}  # agent row id -> its latest estimated cost
    agent_of: dict[str, str] = {}  # agent row id -> name
    counted: set[str] = set()  # decision ids already counted
    for e in events:
        p = e.payload
        if e.type == "task.updated" and p.get("status") == "done" and p.get("owner"):
            tasks.setdefault(p["owner"], set()).add(p["id"])
            worked.setdefault(p["owner"], set()).add(p["stage_id"])
        elif e.type == "verdict.created" and p.get("loop") == 1:
            first[p["stage_id"]] = bool(p.get("passed"))
        elif e.type == "agent.updated":
            agent_of[p["id"]] = p["name"]
            cost[p["id"]] = float(p.get("cost_usd") or 0.0)
        elif e.type == "run.updated":
            stage_ids = {s["name"]: s["id"] for s in p.get("stages") or []}
            for d in p.get("decisions") or []:
                target = stage_ids.get(d.get("send_back_to") or "")
                if target and d["id"] not in counted:
                    counted.add(d["id"])
                    sent_back[target] = sent_back.get(target, 0) + 1
    out: dict[str, Record] = {}
    for name in {*tasks, *agent_of.values()}:
        stages = worked.get(name, set())
        judged = [s for s in stages if s in first]
        out[name] = Record(
            tasks_done=len(tasks.get(name, ())),
            judged=len(judged),
            first_try=sum(first[s] for s in judged),
            send_backs=sum(sent_back.get(s, 0) for s in stages),
            cost_usd=round(sum(c for a, c in cost.items() if agent_of[a] == name), 6),
        )
    return out
