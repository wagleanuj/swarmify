"""A workflow's `schedule`: a cron expression DBOS fires (ADR 0014), checked and projected here.

croniter is what DBOS itself parses schedules with, so a schedule this module accepts is one
DBOS accepts, and the next fire time shown in the editor is the one DBOS will use. Six fields
put seconds first, as DBOS reads them.
"""

from datetime import UTC, datetime

from croniter import croniter

CRON_RULE = "schedule"


def cron_problem(expression: str) -> str | None:
    """Why `expression` is not a cron schedule, or None when it is."""
    if croniter.is_valid(expression, second_at_beginning=True):
        return None
    return "schedule must be a cron expression with 5 fields (or 6 with seconds first)"


def next_fire(expression: str, now: datetime | None = None) -> datetime:
    """The first time after `now` (UTC) the schedule fires. Raises ValueError for a bad one."""
    if cron_problem(expression) is not None:
        raise ValueError(f"not a cron schedule: {expression!r}")
    start = now or datetime.now(UTC)
    return croniter(expression, start, second_at_beginning=True).get_next(datetime)
