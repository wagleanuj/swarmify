"""Session guard for every test path."""

import sys
import threading

import pytest


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Fail the run when a non-daemon thread outlives it: Python joins those at exit, so the
    process would hang after the summary."""
    others = [t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon]
    for thread in others:
        thread.join(5)
    if left := [t.name for t in others if t.is_alive()]:
        sys.stderr.write(f"\nnon-daemon threads outlived the session: {', '.join(left)}\n")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
