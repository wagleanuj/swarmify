"""Budget arithmetic: 80 percent warns, 100 percent stops, no budget never does."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swarmify_runs.budget import level, monthly_cap

MONEY = st.floats(min_value=0, max_value=1e6, allow_nan=False)


def test_the_thresholds() -> None:
    assert level(3.99, 5) == "ok"
    assert level(4.0, 5) == "warning"
    assert level(4.99, 5) == "warning"
    assert level(5.0, 5) == "spent"
    assert level(1e9, None) == "ok"


@given(MONEY, MONEY, st.floats(min_value=0.01, max_value=1e6))
def test_more_spend_never_lowers_the_level(a: float, b: float, budget: float) -> None:
    order = ["ok", "warning", "spent"]
    low, high = sorted((a, b))
    assert order.index(level(low, budget)) <= order.index(level(high, budget))


def test_the_monthly_cap_cuts_a_run_budget_down_to_what_is_left_of_the_month() -> None:
    assert monthly_cap(None, None, 3.0) is None
    assert monthly_cap(2.0, None, 3.0) == 2.0
    assert monthly_cap(None, 10.0, 3.0) == 7.0
    assert monthly_cap(2.0, 10.0, 3.0) == 2.0
    assert monthly_cap(20.0, 10.0, 3.0) == 7.0
    with pytest.raises(ValueError, match="monthly budget of \\$10.00 is spent"):
        monthly_cap(2.0, 10.0, 10.0)
