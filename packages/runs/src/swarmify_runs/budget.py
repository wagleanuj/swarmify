"""Budget arithmetic: the spend ceiling of a run and every child run below it (ADR 0013).

Spend is the Agent SDK's estimate, not billing (V1 spec). At 80 percent a warning goes out, at
100 percent every agent of the run stops and a budget gate asks for more money or an abort.
"""

from typing import Literal

WARN_AT = 0.8
Level = Literal["ok", "warning", "spent"]


def level(spent: float, budget: float | None) -> Level:
    """Where `spent` stands against `budget`; no budget never warns or stops."""
    if budget is None or spent < WARN_AT * budget:
        return "ok"
    return "spent" if spent >= budget else "warning"


def ask(spent: float, budget: float) -> str:
    """What the budget gate asks."""
    return (
        f"The budget of ${budget:.2f} is spent (${spent:.2f} so far, an estimate)."
        " Add money to go on, or abort."
    )
