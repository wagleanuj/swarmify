"""The Eval: the weekly short, as Baseline and as Swarm, over a fixed task set. Built on
Anthropic's agent evaluation guidance ("Demystifying evals for AI agents"): twenty or more
tasks, each trial in a fresh world, outputs graded and not the path taken, and per arm pass at
k (any of k trials passed) and pass to the k (all k passed). The report compares acceptance,
cost and duration of the two arms and says whether the swarm wins, which is the V1 acceptance.

Every check passing on both arms says nothing about which is better, so a blind pairwise judge
compares each task's two outputs with the arm labels removed and the order shuffled per task:
the swarm wins when it wins more than half of the decided pairs (ties left out).

This module is the pure part: the tasks, the grader, the judge's prompt and the report.
`swarmify-eval` (the api app) runs the trials over HTTP and stores the report as an artifact in
the eval world.
"""

import random
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
    """One niche the weekly short is made for. The script must name one of `keywords` (its
    topic words and synonyms), matched by word stem so "biking" names "bike"."""

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
    _task("urban cycling", "bike", "cycling", "cyclist", "ride", "commute"),
    _task("home cooking on a budget", "cook", "meal", "recipe", "dinner", "kitchen", "food"),
    _task("learning Spanish", "spanish", "español", "spain", "language", "fluent"),
    _task("smartphone photography", "photo", "photograph", "camera", "phone", "smartphone"),
    _task("dog training", "dog", "puppy"),
    _task("minimalist living", "minimal", "declutter"),
    _task("chess openings", "chess", "opening"),
    _task("running a first marathon", "marathon", "run"),
    _task("woodworking for beginners", "wood", "woodwork", "saw"),
    _task("productivity for remote workers", "remote", "focus", "work"),
    _task("astronomy from the backyard", "astronomy", "star", "telescope", "planet"),
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


def _stem(word: str) -> str:
    """A crude suffix stripper, the same on both sides of the match: bike, bikes and biking all
    give "bik". ponytail: English suffixes only, a real stemmer if tasks go multilingual."""
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    stripped = True
    while stripped:
        stripped = False
        for suffix in ("ing", "ist", "er", "ed", "es", "s", "y", "e"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word, stripped = word[: -len(suffix)], True
                break
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiou":
        word = word[:-1]
    return word


def _stems(text: str) -> str:
    """The text's words as stems, space separated, so a keyword matches whole words only."""
    return " ".join(_stem(w) for w in re.findall(r"[^\W\d_]+", text.lower()))


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
    stems = f" {_stems(script)} "
    if not any(f" {_stems(k)} " in stems for k in task.keywords):
        failed.append(f"{SCRIPT} never names {task.topic}")
    return failed


def blind_order(task_id: str) -> tuple[str, str]:
    """The arms as the judge sees them, A then B: shuffled per task, the same every time."""
    return ARMS if random.Random(task_id).random() < 0.5 else (ARMS[1], ARMS[0])


PICK_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "better": {"enum": ["A", "B", "tie"]},
        "reason": {"type": "string", "description": "Why, citing what each one did"},
    },
    "required": ["better", "reason"],
}
JUDGE_SYSTEM = (
    "You judge two drafts of a short video for a creator. Answer only with the JSON the schema"
    " asks for."
)


def judge_prompt(task: EvalTask, outputs: dict[str, dict[str, str]]) -> str:
    """Both arms' outputs (arm to artifact name to body) as A and B in the task's blind order."""
    parts = [
        f"Two teams each made {IDEAS} ({MIN_IDEAS} short video ideas about {task.topic}, ranked"
        f" best first, each with a hook) and {SCRIPT} (the spoken script of a 45 to 60 second"
        " short from the top idea). Which pair would you rather publish this week? Weigh how"
        " fresh and specific the ideas are, how strong the hook is and how well the script"
        " holds attention. Say tie only when neither is better."
    ]
    for label, arm in zip("AB", blind_order(task.id), strict=True):
        for name in (IDEAS, SCRIPT):
            body = outputs.get(arm, {}).get(name)
            parts.append(f"## {label}: {name}\n\n{body.strip() if body else '(missing)'}")
    return "\n\n".join(parts)


@dataclass(frozen=True)
class Judgement:
    """The blind judge's pick between the two arms' trials `index` of one task. `winner` is
    the better arm, None for a tie."""

    task_id: str
    index: int
    winner: str | None
    reason: str


def parse_pick(task_id: str, index: int, output: object) -> Judgement:
    """The judge's structured answer, its A or B turned back into the arm."""
    if not isinstance(output, dict) or output.get("better") not in ("A", "B", "tie"):
        raise ValueError("the judge picked neither A, B nor tie")
    if not str(output.get("reason") or "").strip():
        raise ValueError("the judge gave no reason")
    better = output["better"]
    winner = None if better == "tie" else blind_order(task_id)["AB".index(better)]
    return Judgement(task_id, index, winner, str(output["reason"]).strip())


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
    wins: int = 0
    losses: int = 0
    ties: int = 0
    """Pairs the blind judge decided for this arm, against it, and called even."""
    cost_per_win: float | None = None
    """The arm's whole estimated cost over its wins; None without a win."""


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
    judgements: list[Judgement] = field(default_factory=list)


def _arm(arm: str, trials: list[Trial], k: int, judged: list[Judgement]) -> ArmSummary:
    by_task: dict[str, list[Trial]] = {}
    for t in trials:
        by_task.setdefault(t.task_id, []).append(t)
    for task, ts in by_task.items():
        if len(ts) < k:
            raise ValueError(f"task {task} on {arm} has {len(ts)} trials, fewer than k={k}")
    counts = [(len(ts), sum(t.passed for t in ts)) for ts in by_task.values()]
    times = [t.duration_s for t in trials if t.passed and t.duration_s is not None]
    accepted = sum(t.passed for t in trials)
    wins = sum(j.winner == arm for j in judged)
    return ArmSummary(
        arm,
        len(trials),
        accepted,
        accepted / len(trials),
        mean(pass_at_k(n, c, k) for n, c in counts),
        mean(pass_pow_k(n, c, k) for n, c in counts),
        mean(t.cost_usd for t in trials),
        mean(times) if times else None,
        wins,
        sum(j.winner not in (arm, None) for j in judged),
        sum(j.winner is None for j in judged),
        sum(t.cost_usd for t in trials) / wins if wins else None,
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


def _per_win(usd: float | None) -> str:
    return "no win" if usd is None else f"${usd:.2f} a win"


def summarize(
    trials: list[Trial],
    k: int,
    id: str,
    created_at: str,
    judgements: list[Judgement] | None = None,
) -> EvalReport:
    """Raises ValueError unless both arms ran every task at least k times."""
    judged = judgements or []
    arms = {t.arm for t in trials} | {j.winner for j in judged if j.winner is not None}
    if unknown := arms - set(ARMS):
        raise ValueError(f"unknown arm {', '.join(sorted(unknown))}")
    tasks = {t.task_id for t in trials}
    for arm in ARMS:
        missing = tasks - {t.task_id for t in trials if t.arm == arm}
        if missing:
            raise ValueError(f"task {min(missing)} on {arm} has 0 trials, fewer than k={k}")
    base, swarm = (_arm(a, [t for t in trials if t.arm == a], k, judged) for a in ARMS)
    decided = swarm.wins + swarm.losses
    wins = swarm.wins * 2 > decided
    verdict = (
        f"The swarm {'wins' if wins else 'does not win'}: the blind judge preferred it in"
        f" {swarm.wins} of {decided} decided pairs ({swarm.ties} tied), at"
        f" {_per_win(swarm.cost_per_win)} against {_per_win(base.cost_per_win)} for the baseline."
        f" It accepted {_pct(swarm.acceptance)} of trials first try against"
        f" {_pct(base.acceptance)}, in {took(swarm.duration_s)} an accepted trial against"
        f" {took(base.duration_s)}, at ${swarm.cost_usd:.2f} a trial against"
        f" ${base.cost_usd:.2f} (estimated)."
    )
    return EvalReport(id, created_at, k, len(tasks), [base, swarm], wins, verdict, trials, judged)


def markdown(report: EvalReport) -> str:
    k = report.k
    lines = [
        f"# Eval: baseline against swarm, {report.tasks} tasks, k={k}",
        "",
        report.verdict,
        "",
        f"| Arm | Trials | Acceptance | pass@{k} | pass^{k} | Cost (est.) | Duration"
        " | Wins | Losses | Ties | Cost a win |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in report.arms:
        lines.append(
            f"| {a.arm} | {a.trials} | {_pct(a.acceptance)} | {_pct(a.pass_at_k)} |"
            f" {_pct(a.pass_pow_k)} | ${a.cost_usd:.2f} | {took(a.duration_s)} | {a.wins} |"
            f" {a.losses} | {a.ties} | {_per_win(a.cost_per_win)} |"
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
    lines += ["", "| Task | Trial | Judge preferred | Why |", "|---|---|---|---|"]
    for j in report.judgements:
        why = " ".join(j.reason.split()).replace("|", "/")
        lines.append(f"| {j.task_id} | {j.index + 1} | {j.winner or 'tie'} | {why} |")
    return "\n".join(lines) + "\n"
