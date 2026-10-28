"""The eval harness at the API seam: real DBOS on SQLite, the real Agent SDK client with a
scripted CLI behind its transport, fresh worlds per trial, the blind judge per pair, and the
report as an artifact."""

from collections.abc import Iterator
from pathlib import Path

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from swarmify_api.eval_harness import Settings, main, run_eval
from swarmify_api.main import create_app
from swarmify_learning.evals import TASKS, blind_order
from swarmify_memory import FakeGraphIndex
from swarmify_swarm import SdkRuntime, tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Say, Script

TASK = TASKS[0]
FAST = Settings(budget_usd=3.0, timeout_s=30, poll_s=0.05)
IDEAS = "\n".join(f"{n}. Idea {n}: open on the hook" for n in range(1, 6))
SCRIPT = "Hook first. " + "word " * 80 + TASK.keywords[0]
GRAPH = Call(tool_id("query_graph"), {"question": "What worked before?"})
CLAIM = Call(tool_id("claim_task"))
FINISH = Call(tool_id("finish_task"), {"task_id": LAST, "note": "Done."})
PLAN = [Answer({"tasks": [{"title": "Do the stage goal."}]})]
PASS = [Answer({"passed": True, "reasons": ["Meets the done condition."]})]
HOOKS = "---\nname: hooks\ndescription: Open on the hook.\n---\n\nWrite the hook first.\n"
PROPOSAL = {
    "skill": {
        "action": "new",
        "name": "hooks",
        "content": HOOKS,
        "summary": "Hook first",
        "evidence": ["research: passed first time."],
    },
    "workflow": None,
}


def pick(arm: str | None, reason: str = "Sharper hook.") -> list:
    """The judge's session, picking `arm` (None: a tie) by its blind label."""
    better = "tie" if arm is None else "AB"[blind_order(TASK.id).index(arm)]
    return [Answer({"better": better, "reason": reason})]


def write(name: str, content: str) -> Call:
    return Call(tool_id("write_artifact"), {"name": name, "content": content})


def baseline(script: str = SCRIPT) -> list:
    return [
        [CLAIM, write("ideas.md", IDEAS), FINISH, Say("Done.")],
        [CLAIM, write("script.md", script), FINISH, Say("Done.")],
    ]


def swarm() -> list:
    stage = [GRAPH, CLAIM]
    return [
        PLAN,
        [*stage, write("ideas.md", IDEAS), FINISH, Say("Done.")],
        PASS,
        PLAN,
        [*stage, write("script.md", SCRIPT), FINISH, Say("Done.")],
        PASS,
        [Answer(PROPOSAL)],  # the retro proposes a skill; the harness rejects it
    ]


@pytest.fixture
def running(tmp_path: Path, script: Script) -> Iterator[TestClient]:
    app = create_app(
        tmp_path,
        runtime=SdkRuntime(transport=script.transport),
        graph_index=FakeGraphIndex(),
        extract=lambda _: None,
    )
    with TestClient(app) as client:
        yield client


def test_each_trial_runs_in_a_fresh_world_is_graded_on_outputs_and_lands_in_the_report(
    running: TestClient, script: Script
) -> None:
    assert running.get("/api/evals").json() == []
    # trials in order: baseline 1, swarm 1, the judge, baseline 2 (its script is too short),
    # swarm 2, the judge
    script.scripts = [
        *baseline(),
        *swarm(),
        pick("swarm", "Fresher ideas."),
        *baseline("Too short, espresso."),
        *swarm(),
        pick("baseline"),
    ]
    script.cost = 0.1
    said: list[str] = []
    report = run_eval(running, [TASK], 2, FAST, say=said.append)

    trials = report["trials"]
    assert [(t["arm"], t["index"], t["status"]) for t in trials] == [
        ("baseline", 0, "done"),
        ("swarm", 0, "done"),
        ("baseline", 1, "done"),
        ("swarm", 1, "done"),
    ]
    assert len({t["world_id"] for t in trials}) == 4, "a fresh world per trial"
    assert trials[2]["failed"] == ["script.md is 3 words, not 60 to 200"]
    assert [t["failed"] for t in trials if t is not trials[2]] == [[], [], []]
    # two worker sessions a baseline; planner, worker and judge twice plus the retro a swarm
    assert [t["cost_usd"] for t in trials] == pytest.approx([0.2, 0.7, 0.2, 0.7])
    assert all(t["duration_s"] is not None for t in trials)
    assert said[2] == f"{TASK.id} 1 judged: swarm: Fresher ideas."
    assert said[3] == f"{TASK.id} baseline 2: script.md is 3 words, not 60 to 200"

    # the judge saw both pairs' outputs, unlabelled; its A or B came back as the arm
    judge_prompts = [p for p in script.prompts if "## A: ideas.md" in p]
    assert len(judge_prompts) == 2 and "Too short, espresso." in judge_prompts[1]
    assert all("baseline" not in p.lower() and "swarm" not in p.lower() for p in judge_prompts)
    assert [(j["index"], j["winner"]) for j in report["judgements"]] == [
        (0, "swarm"),
        (1, "baseline"),
    ]

    base, arm = report["arms"]
    assert (base["arm"], base["accepted"], base["pass_at_k"], base["pass_pow_k"]) == (
        "baseline", 1, 1.0, 0.0,
    )  # fmt: skip
    assert (arm["arm"], arm["accepted"], arm["pass_at_k"], arm["pass_pow_k"]) == (
        "swarm", 2, 1.0, 1.0,
    )  # fmt: skip
    assert (arm["wins"], arm["losses"], arm["ties"], base["wins"]) == (1, 1, 0, 1)
    assert arm["cost_per_win"] == pytest.approx(1.4) and base["cost_per_win"] == pytest.approx(0.4)
    assert not report["swarm_wins"], "one win of two decided pairs is not more than half"
    assert report["verdict"].startswith("The swarm does not win: the blind judge preferred it in")
    assert report["k"] == 2

    swarm_run = running.get(f"/api/runs/{trials[1]['run_id']}").json()
    assert [s["type"] for s in swarm_run["stages"]] == ["swarm", "swarm", "retro"]
    world = trials[1]["world_id"]
    (rejected,) = running.get(f"/api/worlds/{world}/proposals").json()
    assert rejected["status"] == "rejected"
    assert running.get(f"/api/runs/{trials[0]['run_id']}").json()["baseline"] is True

    assert running.get("/api/evals").json() == [report]
    (evals,) = [w for w in running.get("/api/worlds").json() if w["name"] == "Evals"]
    (card,) = running.get(f"/api/worlds/{evals['id']}/library").json()
    assert (card["type"], card["name"], card["title"]) == ("eval", "report.md", report["verdict"])
    assert "| baseline | 2 | 50% | 100% | 0% | $0.20 |" in card["body"]
    assert "| home-espresso | 1 | swarm | Fresher ideas. |" in card["body"]


def test_a_spent_budget_aborts_the_trial_and_clean_removes_the_trial_worlds(
    running: TestClient, script: Script
) -> None:
    # every tool call's step costs $1.00 against a $0.50 Budget
    script.scripts = [[CLAIM, write("ideas.md", IDEAS)], PLAN, [GRAPH, CLAIM], pick(None)]
    script.usage, script.cost = {"input_tokens": 250_000}, 0.1
    cheap = Settings(budget_usd=0.5, timeout_s=30, poll_s=0.05)
    report = run_eval(running, [TASK], 1, cheap, clean=True)
    assert [(t["arm"], t["status"], t["failed"]) for t in report["trials"]] == [
        ("baseline", "aborted", ["the run aborted"]),
        ("swarm", "aborted", ["the run aborted"]),
    ]
    # the judge still compares what the aborted runs left, and calls it even
    assert [j["winner"] for j in report["judgements"]] == [None]
    assert report["arms"][1]["ties"] == 1 and not report["swarm_wins"]
    assert [w["name"] for w in running.get("/api/worlds").json()] == ["Evals"]


def test_a_report_with_an_arm_missing_is_refused(running: TestClient) -> None:
    trial = {
        "task_id": TASK.id,
        "arm": "baseline",
        "index": 0,
        "world_id": "world_1",
        "run_id": "run_1",
        "status": "done",
        "cost_usd": 0.1,
        "duration_s": 1.0,
    }
    refused = running.post("/api/evals", json={"k": 1, "trials": [trial, trial]})
    assert refused.status_code == 422 and "on swarm has 0 trials" in refused.json()["detail"]
    assert running.get("/api/evals").json() == []


def test_the_command_line_talks_to_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake(http, tasks, k, settings, clean, say):  # noqa: ANN001
        seen.update(base=str(http.base_url), auth=http.auth, tasks=len(tasks), k=k)
        seen.update(budget=settings.budget_usd, clean=clean)
        return {"verdict": "The swarm wins: ...", "id": "eval_1"}

    monkeypatch.setattr("swarmify_api.eval_harness.run_eval", fake)
    monkeypatch.setenv("SWARMIFY_PASSWORD", "pw")
    main(["--api", "http://box:8000", "--k", "2", "--tasks", "3", "--budget", "1.5", "--clean"])
    assert seen["base"] == "http://box:8000" and seen["auth"] is not None
    assert (seen["tasks"], seen["k"], seen["budget"], seen["clean"]) == (3, 2, 1.5, True)


def test_a_trial_past_its_timeout_is_aborted_and_fails_on_the_timeout(
    running: TestClient, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    script.scripts = [[CLAIM, Say("Still thinking.")]]  # never finishes a task
    report = run_eval(running, [TASK], 1, Settings(timeout_s=0.1, poll_s=0.05))
    for t in report["trials"]:
        assert t["status"] == "aborted"
        assert t["failed"] == ["timed out after 0 s", "the run aborted"]

    # an abort the API keeps refusing ends the wait one more timeout later
    post = running.post
    refuse = lambda url, **kw: httpx.Response(409) if url.endswith("/abort") else post(url, **kw)  # noqa: E731
    monkeypatch.setattr(running, "post", refuse)
    report = run_eval(running, [TASK], 1, Settings(timeout_s=0.1, poll_s=0.05))
    for t in report["trials"]:
        assert t["status"] not in ("done", "aborted")
        assert t["failed"] == ["timed out after 0 s", f"the run {t['status']}"]


def test_a_trial_the_api_refused_is_an_error_and_the_report_still_lands(
    running: TestClient,
) -> None:
    report = run_eval(running, [TASK], 1, Settings(budget_usd=-1), clean=True)
    # a negative Budget is refused when the trial saves its workflow
    trials = report["trials"]
    assert [t["status"] for t in trials] == ["error", "error"]
    assert all("422" in t["failed"][0] and t["world_id"] for t in trials)
    assert running.get("/api/evals").json() == [report]
    assert [w["name"] for w in running.get("/api/worlds").json()] == ["Evals"]


def test_a_pair_the_judge_cannot_decide_is_left_out_and_the_judge_route_checks_its_input(
    running: TestClient, script: Script
) -> None:
    script.scripts = [*baseline(), *swarm(), [Say("Both are fine.")]]  # no structured answer
    said: list[str] = []
    report = run_eval(running, [TASK], 1, FAST, say=said.append)
    assert report["judgements"] == [] and f"{TASK.id} 1 judged: undecided" in said
    assert report["arms"][1]["wins"] == 0 and not report["swarm_wins"]

    side = {"world_id": report["trials"][0]["world_id"], "run_id": "run_1"}
    pair = {"task_id": "no-such-task", "baseline": side, "swarm": side}
    assert running.post("/api/evals/judge", json=pair).status_code == 404
    gone = {"world_id": "world_gone", "run_id": "run_1"}
    pair = {"task_id": TASK.id, "baseline": side, "swarm": gone}
    assert running.post("/api/evals/judge", json=pair).status_code == 404
