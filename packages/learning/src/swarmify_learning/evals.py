"""The Eval: starter workflows run as Baseline and as Swarm over a fixed task set. Built on
Anthropic's agent evaluation guidance ("Demystifying evals for AI agents"): twenty or more
tasks, each trial in a fresh world, outputs graded and not the path taken, and per arm pass at
k (any of k trials passed) and pass to the k (all k passed). The report compares acceptance,
cost and duration of the two arms and says whether the swarm wins, which is the V1 acceptance.

The tasks span research briefs, drafts, typed lists and the weekly short (#128), and each names
the starter workflow it exercises; each workflow brings its own run, grader and brief for the
judge.

Every check passing on both arms says nothing about which is better, so a blind pairwise judge
compares each task's two outputs with the arm labels removed and the order shuffled per task.
The baseline arm is one agent a stage, the swarm arm several (ADR 0020). The eval runs per task
kind, and a kind is a swarm win only by the bar fixed in advance (`swarm_wins`): more than 60% of
the decided pairs (ties left out), significant on a one-sided sign test, at most 3 times the
baseline's cost a win.

This module is the pure part: the tasks, the graders, the judge's prompt and the report.
`swarmify-eval` (the api app) runs the trials over HTTP and stores the report as an artifact in
the eval world.
"""

import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from math import comb
from statistics import mean

ARMS = ("baseline", "swarm")
IDEAS, SCRIPT = "ideas.md", "script.md"
MIN_IDEAS = 5
WORDS = (60, 200)
"""A 45 to 60 second short read aloud at 130 to 160 words a minute, with slack either way."""
BRIEF, BRIEF_WORDS, MIN_LINKS = "brief.md", (250, 1000), 3
"""A brief asked for in 300 to 800 words, with slack either way."""
OUTLINE, DRAFT, DRAFT_WORDS = "outline.md", "draft.md", (500, 1200)
"""A draft asked for in 600 to 1000 words, with slack either way."""
FINDING, FINDINGS = "finding-", (3, 10)
"""A `list[finding]` projects one `finding-<title>.md` artifact an item."""
_LINK = re.compile(r"https?://[^\s)>\]]+")


@dataclass(frozen=True)
class EvalTask:
    """One topic `workflow` (a starter workflow's name) is run on. Its output must name one of
    `keywords` (its topic words and synonyms), matched by word stem so "biking" names "bike"."""

    id: str
    topic: str
    keywords: tuple[str, ...]
    workflow: str


def _tasks(workflow: str, *topics: tuple[str, ...]) -> list[EvalTask]:
    """One task a topic, each `(topic, *keywords)`."""
    slug = lambda topic: re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")  # noqa: E731
    return [EvalTask(slug(t[0]), t[0], t[1:], workflow) for t in topics]


TASKS = [
    *_tasks(
        "Weekly short",
        ("home espresso", "espresso", "coffee"),
        ("urban cycling", "bike", "cycling", "cyclist", "ride", "commute"),
        ("home cooking on a budget", "cook", "meal", "recipe", "dinner", "kitchen", "food"),
        ("learning Spanish", "spanish", "español", "spain", "language", "fluent"),
        ("running a first marathon", "marathon", "run"),
        ("astronomy from the backyard", "astronomy", "star", "telescope", "planet"),
    ),
    *_tasks(
        "Research brief",
        ("heat pumps in cold climates", "heat pump", "cold", "winter"),
        ("four-day work weeks", "four-day", "week", "workweek"),
        ("open source licensing", "license", "licence", "licensing", "open source"),
        ("e-bike battery safety", "battery", "bike", "lithium"),
        ("microplastics in drinking water", "microplastic", "plastic", "water"),
        ("phishing at small businesses", "phishing", "security", "small business"),
        ("home battery storage", "battery", "storage", "solar"),
        ("screen time and sleep", "screen", "sleep"),
    ),
    *_tasks(
        "Draft with approval",
        ("a beginner's guide to composting", "compost"),
        ("why cities should plant more trees", "tree", "canopy"),
        ("how to run a shorter meeting", "meeting", "agenda"),
        ("the case for public libraries", "library", "librarian"),
        ("starting a household budget", "budget", "money", "spend"),
        ("writing clear documentation", "documentation", "docs", "readme"),
        ("the value of a daily walk", "walk", "walking", "step"),
        ("learning to cook at home", "cook", "kitchen", "recipe", "meal"),
    ),
    *_tasks(
        "Scheduled monitor",
        ("electric vehicle charging", "charging", "charger", "electric", "ev"),
        ("AI regulation", "regulation", "law", "act", "ai"),
        ("renewable energy prices", "solar", "wind", "energy", "renewable"),
        ("space launches", "launch", "rocket", "orbit"),
        ("remote work policies", "remote", "office", "hybrid"),
        ("open source security advisories", "vulnerability", "security", "cve", "advisory"),
        ("web browser releases", "browser", "chrome", "firefox", "safari", "release"),
        ("the housing market", "housing", "home", "mortgage", "rent"),
    ),
]
KINDS = {
    "research briefs": "Research brief",
    "long-form writing": "Draft with approval",
    "typed lists": "Scheduled monitor",
}
"""The task kinds the swarm evidence eval decides per (ADR 0020), each to the workflow its
tasks run. The weekly short is not one: the V1 acceptance eval already measured it."""


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


def _names(task: EvalTask, text: str) -> bool:
    stems = f" {_stems(text)} "
    return any(f" {_stems(k)} " in stems for k in task.keywords)


def _length(name: str, body: str, words: tuple[int, int]) -> list[str]:
    n = len(body.split())
    if words[0] <= n <= words[1]:
        return []
    return [f"{name} is {n} words, not {words[0]} to {words[1]}"]


def _grade_short(task: EvalTask, artifacts: dict[str, str]) -> list[str]:
    failed = []
    ideas = artifacts.get(IDEAS)
    if ideas is None:
        failed.append(f"{IDEAS} is missing")
    elif (n := len(_NUMBERED.findall(ideas))) < MIN_IDEAS:
        failed.append(f"{IDEAS} ranks {n} ideas, not {MIN_IDEAS}")
    script = artifacts.get(SCRIPT)
    if script is None:
        return [*failed, f"{SCRIPT} is missing"]
    failed += _length(SCRIPT, script, WORDS)
    if not _names(task, script):
        failed.append(f"{SCRIPT} never names {task.topic}")
    return failed


def _grade_brief(task: EvalTask, artifacts: dict[str, str]) -> list[str]:
    brief = artifacts.get(BRIEF)
    if brief is None:
        return [f"{BRIEF} is missing"]
    failed = _length(BRIEF, brief, BRIEF_WORDS)
    if (n := len(set(_LINK.findall(brief)))) < MIN_LINKS:
        failed.append(f"{BRIEF} cites {n} links, not {MIN_LINKS}")
    if not _names(task, brief):
        failed.append(f"{BRIEF} never names {task.topic}")
    return failed


def _grade_draft(task: EvalTask, artifacts: dict[str, str]) -> list[str]:
    failed = [] if OUTLINE in artifacts else [f"{OUTLINE} is missing"]
    draft = artifacts.get(DRAFT)
    if draft is None:
        return [*failed, f"{DRAFT} is missing"]
    failed += _length(DRAFT, draft, DRAFT_WORDS)
    if not _names(task, draft):
        failed.append(f"{DRAFT} never names {task.topic}")
    return failed


def _grade_findings(task: EvalTask, artifacts: dict[str, str]) -> list[str]:
    found = [body for name, body in artifacts.items() if name.startswith(FINDING)]
    if not FINDINGS[0] <= len(found) <= FINDINGS[1]:
        return [f"{len(found)} findings, not {FINDINGS[0]} to {FINDINGS[1]}"]
    failed = []
    if unlinked := sum(not _LINK.search(body) for body in found):
        failed.append(f"{unlinked} of {len(found)} findings have no source link")
    if not _names(task, "\n".join(found)):
        failed.append(f"no finding names {task.topic}")
    return failed


@dataclass(frozen=True)
class EvalWorkflow:
    """A starter workflow as the eval runs it: no gate, no schedule, no render and no publish,
    since a trial must never post anything and nobody answers a gate at night. `yaml` takes the
    topic and the budget; `files` are what it leaves, a name ending in `-` the prefix of a typed
    list's items; `made` and `weigh` tell the judge what each team made and what to weigh."""

    name: str
    yaml: str
    files: tuple[str, ...]
    grade: Callable[[EvalTask, dict[str, str]], list[str]]
    made: str
    weigh: str


WORKFLOWS = {
    w.name: w
    for w in [
        EvalWorkflow(
            "Weekly short",
            f"""name: Weekly short eval, {{topic}}
budget_usd: {{budget}}
stages:
  - swarm: research
    goal: Find {MIN_IDEAS} short video ideas about {{topic}} that would work this week.
    done_when: {IDEAS} ranks {MIN_IDEAS} ideas as a numbered list, best first, each with a hook
    tools: [web_search, graph]
    produces: [{IDEAS}]
  - swarm: script
    goal: Write the script of a 45 to 60 second short about {{topic}} from the top idea.
    done_when: {SCRIPT} is the spoken script, {WORDS[0]} to {WORDS[1]} words, opening on a hook
    tools: [graph]
    produces: [{SCRIPT}]
""",
            (IDEAS, SCRIPT),
            _grade_short,
            f"{IDEAS} ({MIN_IDEAS} short video ideas about {{topic}}, ranked best first, each"
            f" with a hook) and {SCRIPT} (the spoken script of a 45 to 60 second short from the"
            " top idea).",
            "Which pair would you rather publish this week? Weigh how fresh and specific the"
            " ideas are, how strong the hook is and how well the script holds attention.",
        ),
        EvalWorkflow(
            "Research brief",
            f"""name: Research brief eval, {{topic}}
budget_usd: {{budget}}
stages:
  - swarm: research
    goal: Research {{topic}}. Check the graph for what this world already knows,
      then the web for the rest.
    done_when: {BRIEF} answers the question in 300 to 800 words and cites at least
      {MIN_LINKS} sources by link
    tools: [web_search, graph]
    produces: [{BRIEF}]
""",
            (BRIEF,),
            _grade_brief,
            f"{BRIEF} (a cited brief on {{topic}} in 300 to 800 words).",
            "Which brief would you rather rely on? Weigh how accurate, specific and current it"
            " is, and how well its sources back it.",
        ),
        EvalWorkflow(
            "Draft with approval",
            f"""name: Draft with approval eval, {{topic}}
budget_usd: {{budget}}
stages:
  - swarm: outline
    goal: Gather the points and sources a piece on {{topic}} needs. Check the graph first.
    done_when: {OUTLINE} lists the sections in order, each with its key points and sources
    tools: [web_search, graph]
    produces: [{OUTLINE}]
  - swarm: draft
    goal: Write the piece on {{topic}} from {OUTLINE}.
    done_when: {DRAFT} is a complete draft of 600 to 1000 words under a title
    tools: [graph]
    produces: [{DRAFT}]
""",
            (OUTLINE, DRAFT),
            _grade_draft,
            f"{OUTLINE} and {DRAFT} (a piece on {{topic}} in 600 to 1000 words).",
            "Which draft would you rather approve as it stands? Weigh how clear, correct and"
            " well structured it is, and how little it would need editing.",
        ),
        EvalWorkflow(
            "Scheduled monitor",
            f"""name: Scheduled monitor eval, {{topic}}
budget_usd: {{budget}}
stages:
  - swarm: scan
    goal: Find what changed this week in {{topic}}.
    done_when: {FINDINGS[0]} to {FINDINGS[1]} findings, each with what changed, why it
      matters and a source link
    tools: [web_search, graph]
    output_type: list[finding]
""",
            (FINDING,),
            _grade_findings,
            f"a typed list of {FINDINGS[0]} to {FINDINGS[1]} findings on what changed this week"
            " in {topic}, one item each.",
            "Which list would you rather get in your inbox? Weigh how new, relevant and well"
            " sourced the findings are, and whether anything important is missing.",
        ),
    ]
}


def workflow_yaml(task: EvalTask, budget_usd: float) -> str:
    """The task's workflow as the eval runs it, on the task's topic."""
    return WORKFLOWS[task.workflow].yaml.format(topic=task.topic, budget=budget_usd)


def made_by(task: EvalTask, name: str) -> bool:
    """Whether artifact `name` is one the task's workflow leaves, so the judge reads it."""
    return any(
        name.startswith(f) if f.endswith("-") else name == f for f in WORKFLOWS[task.workflow].files
    )


def grade(task: EvalTask, artifacts: dict[str, str]) -> list[str]:
    """The checks the trial's outputs failed, by artifact name to body; none means accepted."""
    return WORKFLOWS[task.workflow].grade(task, artifacts)


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
    "You judge two teams' work on the same task. Answer only with the JSON the schema asks for."
)


def judge_prompt(task: EvalTask, outputs: dict[str, dict[str, str]]) -> str:
    """Both arms' outputs (arm to artifact name to body) as A and B in the task's blind order."""
    flow = WORKFLOWS[task.workflow]
    parts = [
        f"Two teams each made {flow.made.format(topic=task.topic)} {flow.weigh} Say tie only"
        " when neither is better."
    ]
    for label, arm in zip("AB", blind_order(task.id), strict=True):
        made = outputs.get(arm, {})
        for f in flow.files:
            names = sorted(n for n in made if n.startswith(f)) if f.endswith("-") else [f]
            for name in names or [f"{f}*"]:
                body = made.get(name)
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
    kind: str | None = None
    """The task kind (a key of KINDS) the eval ran; None for a mixed or older report."""
    p_value: float | None = None
    """The sign test's one-sided p of the swarm's share of the decided pairs."""


SHARE, ALPHA, COST_RATIO = 0.6, 0.05, 3
"""The bar a kind must clear to be a swarm win, fixed before any eval ran (ADR 0020)."""


def sign_test(wins: int, decided: int) -> float:
    """One-sided p of `wins` or more of `decided` pairs if either arm were as likely to win."""
    return sum(comb(decided, i) for i in range(wins, decided + 1)) / 2**decided


def swarm_wins(swarm: ArmSummary, baseline: ArmSummary) -> bool:
    """More than SHARE of the decided pairs, sign test p under ALPHA, and a cost a win at most
    COST_RATIO times the baseline's (any cost passes when the baseline won nothing)."""
    decided = swarm.wins + swarm.losses
    if swarm.cost_per_win is None or swarm.wins <= SHARE * decided:
        return False
    if sign_test(swarm.wins, decided) >= ALPHA:
        return False
    return baseline.cost_per_win is None or swarm.cost_per_win <= COST_RATIO * baseline.cost_per_win


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
    kind: str | None = None,
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
    wins = swarm_wins(swarm, base)
    p = sign_test(swarm.wins, decided)
    verdict = (
        f"The swarm {'wins' if wins else 'does not win'}{f' on {kind}' if kind else ''}: the blind"
        f" judge preferred it in {swarm.wins} of {decided} decided pairs ({swarm.ties} tied,"
        f" p = {p:.2f}), at {_per_win(swarm.cost_per_win)} against {_per_win(base.cost_per_win)}"
        " for the baseline."
        f" It accepted {_pct(swarm.acceptance)} of trials first try against"
        f" {_pct(base.acceptance)}, in {took(swarm.duration_s)} an accepted trial against"
        f" {took(base.duration_s)}, at ${swarm.cost_usd:.2f} a trial against"
        f" ${base.cost_usd:.2f} (estimated)."
    )
    return EvalReport(
        id, created_at, k, len(tasks), [base, swarm], wins, verdict, trials, judged, kind, p
    )


def markdown(report: EvalReport) -> str:
    k = report.k
    lines = [
        f"# Eval{f' of {report.kind}' if report.kind else ''}: baseline against swarm,"
        f" {report.tasks} tasks, k={k}",
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
