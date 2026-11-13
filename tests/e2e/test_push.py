"""Settings, Notifications (issue 459) in the built app with its service worker, up to the
browser's permission step: the permission answer and the PushManager are mocked, so the
subscription points at a push service that does not exist, and the test button reports that."""

import httpx
from playwright.sync_api import Page, expect

FAKE_PUSH = """
(() => {
  // A real P-256 point (the generator): the server refuses keys it could never encrypt for.
  const keys = {
    p256dh: "BGsX0fLhLEJH-Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT-NC4v4af5uO5-tK" +
      "fA-eFivOM1drMV7Oy7ZAaDe_UfU",
    auth: "A".repeat(22),
  };
  let sub = null;
  const make = () => ({
    endpoint: "https://push.invalid/e2e",
    toJSON: () => ({ endpoint: "https://push.invalid/e2e", keys }),
    unsubscribe: async () => { sub = null; return true; },
  });
  PushManager.prototype.getSubscription = async function () { return sub; };
  PushManager.prototype.subscribe = async function () { return (sub = make()); };
  // Headless Chromium denies notifications outright: the browser's answer is mocked too.
  let permission = "default";
  Object.defineProperty(Notification, "permission", { get: () => permission });
  Notification.requestPermission = async () => (permission = "granted");
})();
"""


def test_turn_on_test_and_turn_off_notifications_on_this_device(page: Page, base_url: str) -> None:
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Push"}).json()["id"]
    try:
        page.add_init_script(FAKE_PUSH)
        page.goto(f"{base_url}/worlds/{world}/settings")
        page.wait_for_function("navigator.serviceWorker.ready.then(() => true)")
        panel = page.get_by_role("region", name="Notifications")
        state = panel.get_by_test_id("push-state")
        expect(state).to_have_text("Off on this device.")

        panel.get_by_role("button", name="Turn on for this device").click()
        expect(state).to_contain_text("On for this device, Chrome")
        devices = httpx.get(f"{base_url}/api/push/subscriptions").json()
        assert len(devices) == 1 and "endpoint" not in devices[0]
        expect(panel.get_by_role("checkbox", name="Gates")).to_be_checked()

        panel.get_by_role("button", name="Send a test").click()
        expect(panel.get_by_role("alert")).to_contain_text("push service did not take it")

        panel.get_by_role("button", name="Turn off").click()
        expect(state).to_have_text("Off on this device.")
        assert httpx.get(f"{base_url}/api/push/subscriptions").json() == []
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world}")
