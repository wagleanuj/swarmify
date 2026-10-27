"""The Eval: the weekly short, as Baseline and as Swarm, over a fixed task set. Built on
Anthropic's agent evaluation guidance ("Demystifying evals for AI agents"): twenty or more
tasks, each trial in a fresh world, outputs graded and not the path taken, and per arm pass at
k (any of k trials passed) and pass to the k (all k passed). The report compares acceptance,
cost and duration of the two arms and says whether the swarm wins: it must accept at least as
often as the baseline and reach an accepted output faster, which is the V1 acceptance.

This module is the pure part: the tasks, the grader and the report. `swarmify-eval` (the api
app) runs the trials over HTTP and stores the report as an artifact in the eval world.
"""

import re
from dataclasses import dataclass, field
from math import comb
from statistics import mean

ARMS = ("baseline", "swarm")
IDEAS, SCRIPT = "ideas.md", "script.md"
MIN_IDEAS = 5
WORDS = (60, 200)
"""A 45 to 60 second short read aloud at 130 to 160 words a minute, with slack either way."""


@dataclass(frozen=True)
class EvalTask:
    """One niche the weekly short is made for. The script must name one of `keywords`."""

    id: str
    topic: str
    keywords: tuple[str, ...]


def _task(topic: str, *keywords: str) -> EvalTask:
    return EvalTask(re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-"), topic, keywords)


TASKS = [
    _task("home espresso", "espresso", "coffee"),
    _task("houseplant care", "plant", "houseplant"),
    _task("budget travel in Japan", "japan", "tokyo", "kyoto"),
    _task("beginner strength training", "strength", "lift", "squat"),
    _task("sourdough baking", "sourdough", "starter", "bread"),
    _task("personal finance for students", "budget", "student", "money"),
    _task("indie game development", "game", "indie"),
    _task("urban cycling", "bike", "cycling", "cyclist"),
    _task("home cooking on a budget", "cook", "meal", "recipe"),
    _task("learning Spanish", "spanish", "español"),
    _task("smartphone photography", "photo", "camera", "phone"),
    _task("dog training", "dog", "puppy"),
    _task("minimalist living", "minimal", "declutter"),
    _task("chess openings", "chess", "opening"),
    _task("running a first marathon", "marathon", "run"),
    _task("woodworking for beginners", "wood", "saw"),
    _task("productivity for remote workers", "remote", "focus", "work"),
    _task("astronomy from the backyard", "star", "telescope", "planet"),
    _task("vegan meal prep", "vegan", "plant-based"),
    _task("retro video game collecting", "retro", "cartridge", "console"),
    _task("small space gardening", "garden", "balcony", "grow"),
    _task("sleep hygiene", "sleep", "bedtime"),
]


def workflow_yaml(task: EvalTask, budget_usd: float) -> str:
    """The weekly short as the eval runs it: research and script, no gate, no render and no
    publish, since a trial must never post anything and nobody answers a pick at night."""
    return f"""name: Weekly short eval, {task.topic}
budget_usd: {budget_usd}
stages:
  - swarm: research
    goal: Find {MIN_IDEAS} short video ideas about {task.topic} that would work this week.
    done_when: {IDEAS} ranks {MIN_IDEAS} ideas as a numbered list, best first, each with a hook
    tools: [web_search, graph]
    produces: [{IDEAS}]
  - swarm: script
    goal: Write the script of a 45 to 60 second short about {task.topic} from the top idea.
    done_when: {SCRIPT} is the spoken script, {WORDS[0]} to {WORDS[1]} words, opening on a hook
    tools: [graph]
    produces: [{SCRIPT}]
"""


_NUMBERED = re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE)


def grade(task: EvalTask, artifacts: dict[str, str]) -> list[str]:
    """The checks the trial's outputs failed, by artifact name to body; none means accepted."""
    failed = []
    ideas = artifacts.get(IDEAS)
    if ideas is None:
        failed.append(f"{IDEAS} is missing")
    elif (n := len(_NUMBERED.findall(ideas))) < MIN_IDEAS:
        failed.append(f"{IDEAS} ranks {n} ideas, not {MIN_IDEAS}")
    script = artifacts.get(SCRIPT)
    if script is None:
        return [*failed, f"{SCRIPT} is missing"]
    words = len(script.split())
    if not WORDS[0] <= words <= WORDS[1]:
        failed.append(f"{SCRIPT} is {words} words, not {WORDS[0]} to {WORDS[1]}")
    if not any(k.lower() in script.lower() for k in task.keywords):
        failed.append(f"{SCRIPT} never names {task.topic}")
    return failed


@dataclass(frozen=True)
class Trial:
    """One run of one arm on one task, in its own world. `cost_usd` is the runtime's estimate;
    `duration_s` is None when the run never finished a stage."""

    task_id: str
    arm: str
    index: int
    world_id: str
    run_id: str
    status: str
    cost_usd: float
    duration_s: float | None
    failed: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "done" and not self.failed


def pass_at_k(n: int, c: int, k: int) -> float:
    """The chance at least one of k trials passes, from c passes in n trials (unbiased)."""
    return 1 - comb(n - c, k) / comb(n, k)


def pass_pow_k(n: int, c: int, k: int) -> float:
    """The chance all k trials pass, from c passes in n trials."""
    return comb(c, k) / comb(n, k)


@dataclass(frozen=True)
class ArmSummary:
    arm: str
    trials: int
    accepted: int
    acceptance: float
    pass_at_k: float
    pass_pow_k: float
    cost_usd: float
    """Mean estimated cost a trial."""
    duration_s: float | None
    """Mean over the accepted trials, so a run that fails fast never looks fast; None when
    none was accepted."""


@dataclass(frozen=True)
class EvalReport:
    id: str
    created_at: str
    k: int
    tasks: int
    arms: list[ArmSummary]
    swarm_wins: bool
    verdict: str
    trials: list[Trial]


def _arm(arm: str, trials: list[Trial], k: int) -> ArmSummary:
    by_task: dict[str, list[Trial]] = {}
    for t in trials:
        by_task.setdefault(t.task_id, []).append(t)
    for task, ts in by_task.items():
        if len(ts) < k:
            raise ValueError(f"task {task} on {arm} has {len(ts)} trials, fewer than k={k}")
    counts = [(len(ts), sum(t.passed for t in ts)) for ts in by_task.values()]
    times = [t.duration_s for t in trials if t.passed and t.duration_s is not None]
    accepted = sum(t.passed for t in trials)
    return ArmSummary(
        arm,
        len(trials),
        accepted,
        accepted / len(trials),
        mean(pass_at_k(n, c, k) for n, c in counts),
        mean(pass_pow_k(n, c, k) for n, c in counts),
        mean(t.cost_usd for t in trials),
        mean(times) if times else None,
    )


def took(seconds: float | None) -> str:
    if seconds is None:
        return "none"
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def summarize(trials: list[Trial], k: int, id: str, created_at: str) -> EvalReport:
    """Raises ValueError unless both arms ran every task at least k times."""
    if unknown := {t.arm for t in trials} - set(ARMS):
        raise ValueError(f"unknown arm {', '.join(sorted(unknown))}")
    tasks = {t.task_id for t in trials}
    for arm in ARMS:
        missing = tasks - {t.task_id for t in trials if t.arm == arm}
        if missing:
            raise ValueError(f"task {min(missing)} on {arm} has 0 trials, fewer than k={k}")
    base, swarm = (_arm(a, [t for t in trials if t.arm == a], k) for a in ARMS)
    faster = (
        swarm.duration_s is not None
        and base.duration_s is not None
        and swarm.duration_s < base.duration_s
    )
    wins = faster and swarm.acceptance >= base.acceptance
    verdict = (
        f"The swarm {'wins' if wins else 'does not win'}: it accepted {_pct(swarm.acceptance)}"
        f" of trials against {_pct(base.acceptance)} for the baseline, in"
        f" {took(swarm.duration_s)} an accepted trial against {took(base.duration_s)}, at"
        f" ${swarm.cost_usd:.2f} a trial against ${base.cost_usd:.2f} (estimated)."
    )
    return EvalReport(id, created_at, k, len(tasks), [base, swarm], wins, verdict, trials)


def markdown(report: EvalReport) -> str:
    k = report.k
    lines = [
        f"# Eval: baseline against swarm, {report.tasks} tasks, k={k}",
        "",
        report.verdict,
        "",
        f"| Arm | Trials | Acceptance | pass@{k} | pass^{k} | Cost (est.) | Duration |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in report.arms:
        lines.append(
            f"| {a.arm} | {a.trials} | {_pct(a.acceptance)} | {_pct(a.pass_at_k)} |"
            f" {_pct(a.pass_pow_k)} | ${a.cost_usd:.2f} | {took(a.duration_s)} |"
        )
    lines += [
        "",
        "Cost is the runtime's estimate, not billing. A trial is accepted when its run finished"
        " and its outputs passed every check.",
        "",
        "| Task | Arm | Trial | Status | Cost | Duration | Failed checks | World |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in report.trials:
        lines.append(
            f"| {t.task_id} | {t.arm} | {t.index + 1} | {t.status} | ${t.cost_usd:.2f} |"
            f" {took(t.duration_s)} | {'; '.join(t.failed) or 'none'} | {t.world_id} |"
        )
    return "\n".join(lines) + "\n"
