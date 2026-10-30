"""Starter workflows: what the editor offers a new workflow, so the first one an operator sees is
not only for video (#128). Each uses stage types and tools every world has, so each passes the
workflow check as it stands; `<your topic>` and `<your question>` are for the operator to fill."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Starter:
    name: str
    about: str
    yaml: str


STARTERS = [
    Starter(
        "Research brief",
        "One swarm researches a question and writes a cited brief.",
        """name: Research brief
budget_usd: 3
stages:
  - swarm: research
    goal: Research <your question>. Check the graph for what this world already knows,
      then the web for the rest.
    done_when: brief.md answers the question in 300 to 800 words and cites at least 3
      sources by link
    tools: [web_search, graph]
    produces: [brief.md]
""",
    ),
    Starter(
        "Draft with approval",
        "Outline, draft, then you approve it or send it back with a note.",
        """name: Draft with approval
budget_usd: 4
stages:
  - swarm: outline
    goal: Gather the points and sources a piece on <your topic> needs. Check the graph first.
    done_when: outline.md lists the sections in order, each with its key points and sources
    tools: [web_search, graph]
    produces: [outline.md]
  - swarm: draft
    goal: Write the piece on <your topic> from outline.md, in this world's voice.
    done_when: draft.md is a complete draft of 600 to 1000 words under a title
    tools: [graph]
    produces: [draft.md]
  - gate: review
    ask: Approve the draft, or send it back with a note.
    kind: approve
""",
    ),
    Starter(
        "Scheduled monitor",
        "Every Monday at 08:00 UTC, a typed list of what changed in a topic.",
        """name: Scheduled monitor
schedule: 0 8 * * 1
budget_usd: 2
stages:
  - swarm: scan
    goal: Find what changed this week in <your topic>. Check the graph for what was
      already reported and leave it out.
    done_when: 3 to 10 findings, each with what changed, why it matters and a source link
    tools: [web_search, graph]
    output_type: list[finding]
""",
    ),
    Starter(
        "Weekly short",
        "Five ranked video ideas, then you pick one.",
        """name: Weekly short
budget_usd: 5
stages:
  - swarm: research
    goal: Find 5 short-form video ideas trending this week. Check the graph for what we
      already published.
    done_when: 5 ideas ranked, each with a hook line and a why
    tools: [web_search, graph]
    output_type: list[idea]
  - gate: pick
    ask: Pick one idea. Optional note for the script.
    kind: pick
""",
    ),
]
