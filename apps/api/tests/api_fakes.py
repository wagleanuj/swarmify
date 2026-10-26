"""Shared helpers for the API tests: a polling wait and the recording Notifier fake."""

import time
from collections.abc import Callable

from swarmify_kernel import Event


def until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


class FakeNotifier:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        self.events.append(event)
