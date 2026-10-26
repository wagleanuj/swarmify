"""The Slack adapter of the Notifier port, against a fake Slack endpoint on localhost."""

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError

import pytest

from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel import Event
from swarmify_kernel.sqlite import connect, migrate
from swarmify_notifications import MIGRATIONS, Notifications, SqliteStorage, slack
from swarmify_notifications.fake import FakeSlack

BASE = "http://swarmify.lan:8000/"
RUN = "http://swarmify.lan:8000/worlds/world_1/runs/run_1"
GATES = "http://swarmify.lan:8000/gates"


def event(type: str, **payload: object) -> Event:
    return Event.new(
        type, world_id="world_1", run_id="run_1", payload={"run_id": "run_1", **payload}
    )


@pytest.fixture
def fake() -> Iterator[FakeSlack]:
    with FakeSlack() as fake:
        yield fake


@pytest.fixture
def notifications(tmp_path: Path) -> Notifications:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    return Notifications(SqliteStorage(db), BASE)


def test_each_notification_says_what_happened_with_a_deep_link() -> None:
    gate = event("gate.opened", name="Weekly short", stage="pick", kind="pick", ask="Pick one.")
    paused = event("gate.opened", name="Weekly short", stage="pick", kind="budget", ask="Add $5?")
    warned = event("budget.warning", name="Weekly short", spent_usd=4.1, budget_usd=5)
    assert slack.message(gate, BASE) == (
        f"*Weekly short* is waiting on you at gate pick. Pick one. <{GATES}|Answer the gate>"
    )
    assert slack.message(paused, BASE) == (
        f"*Weekly short* is paused, its budget is spent. Add $5? <{GATES}|Add money or abort>"
    )
    assert slack.message(warned, BASE) == (
        "*Weekly short* has spent $4.10 of its $5.00 budget. It pauses at 100 percent."
        f" <{RUN}|Open the run>"
    )
    for status, said in [("done", "is done"), ("failed", "failed"), ("aborted", "was aborted")]:
        ended = event("run.ended", name="Weekly short", status=status)
        assert slack.message(ended, BASE) == f"*Weekly short* {said}. <{RUN}|Open the run>"


def test_names_cannot_inject_slack_links_and_other_events_are_not_notifications() -> None:
    ended = event("run.ended", name="<https://evil|click> & go", status="done")
    assert slack.message(ended, BASE) == (
        f"*&lt;https://evil|click&gt; &amp; go* is done. <{RUN}|Open the run>"
    )
    assert slack.message(event("run.updated", name="x"), BASE) is None


def test_notify_posts_to_the_worlds_webhook_only(
    notifications: Notifications, fake: FakeSlack
) -> None:
    notifications.notify(event("run.ended", name="Quiet", status="done"))  # no webhook yet
    notifications.set_webhook("world_1", f"  {fake.url}  ")
    assert notifications.webhook("world_1") == fake.url
    notifications.notify(event("run.updated", name="Ignored"))
    notifications.notify(event("run.ended", name="Loud", status="failed"))
    other = Event.new("run.ended", world_id="world_2", payload={"run_id": "r", "status": "done"})
    notifications.notify(other)
    assert fake.posts == [{"text": f"*Loud* failed. <{RUN}|Open the run>"}]


def test_a_refusing_webhook_raises_and_a_bad_url_is_rejected(
    notifications: Notifications, fake: FakeSlack
) -> None:
    notifications.set_webhook("world_1", fake.url)
    fake.status = 404
    with pytest.raises(HTTPError):
        notifications.notify(event("run.ended", name="Lost", status="done"))
    for bad in ["file:///etc/passwd", "hooks.slack.com/services/x", "https://"]:
        with pytest.raises(ValueError):
            notifications.set_webhook("world_1", bad)
    assert notifications.webhook("world_1") == fake.url


def test_the_test_message_and_clearing_the_channel(
    notifications: Notifications, fake: FakeSlack
) -> None:
    with pytest.raises(LookupError):
        notifications.send_test("world_1")
    notifications.set_webhook("world_1", fake.url)
    notifications.set_webhook("world_1", fake.url)  # saving again replaces, not duplicates
    notifications.send_test("world_1")
    assert fake.texts() == [f"Swarmify is connected. <{GATES}|Open gates>"]
    notifications.set_webhook("world_1", "")
    assert notifications.webhook("world_1") is None
    notifications.set_webhook("world_1", fake.url)
    notifications.delete_world("world_1")
    assert notifications.webhook("world_1") is None
