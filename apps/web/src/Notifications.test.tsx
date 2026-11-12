import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { render } from "vitest-browser-react";

import "./index.css";

import { NotificationsRow, PushPrompt } from "./Notifications";
import { deviceLabel, snoozed } from "./push";
import { markAnswered, setPwa } from "./pwa";
import { fakeFetch } from "./test/fakes";
import { answerConfirm } from "./test/mount";

const Row = ({ title, children }: { title: string; children: React.ReactNode }) => (
  <section aria-label={title}>{children}</section>
);
const device = (id: string, label: string, more = {}) => ({
  id,
  label,
  gates: true,
  failures: true,
  budget: true,
  notices: true,
  created_at: "2026-11-10T10:00:00+00:00",
  last_success_at: null,
  ...more,
});
const P256DH = "B" + "A".repeat(86);

/** A browser with a service worker and a PushManager, its permission `permission`. */
function browser(permission: NotificationPermission, subscribed = false) {
  let current = permission;
  let sub: PushSubscription | null = null;
  const make = () =>
    ({
      endpoint: "https://push.example/me",
      toJSON: () => ({ endpoint: "https://push.example/me", keys: { p256dh: P256DH, auth: "A".repeat(22) } }),
      unsubscribe: vi.fn(async () => {
        sub = null;
        return true;
      }),
    }) as unknown as PushSubscription;
  if (subscribed) sub = make();
  const subscribe = vi.fn(async () => (sub = make()));
  const pushManager = { getSubscription: async () => sub, subscribe };
  vi.spyOn(navigator.serviceWorker, "getRegistration").mockResolvedValue({ pushManager } as unknown as ServiceWorkerRegistration);
  vi.spyOn(Notification, "permission", "get").mockImplementation(() => current);
  const ask = vi.spyOn(Notification, "requestPermission").mockImplementation(async () => {
    current = current === "default" ? "granted" : current;
    return current;
  });
  return { subscribe, ask };
}

beforeEach(() => {
  localStorage.clear();
  setPwa({ waiting: null, offer: null, online: true, office: false, answeredNow: false });
});
afterEach(() => {
  vi.restoreAllMocks();
});

test("turns on for this device, saves kinds, sends a test and removes another device", async () => {
  const { subscribe, ask } = browser("default");
  const phone = device("dev_phone", "Chrome on Android", { last_success_at: "2026-11-11T10:00:00+00:00" });
  let mine = device("dev_me", "Chrome on Mac");
  const calls = fakeFetch({
    "GET /api/push/subscriptions": () => Response.json([phone, mine]),
    "GET /api/push/key": () => Response.json({ public_key: P256DH }),
    "POST /api/push/subscriptions": () => Response.json(mine),
    "PATCH /api/push/subscriptions/dev_me": async (req) => {
      mine = { ...mine, ...((await req.json()) as object) };
      return Response.json(mine);
    },
    "POST /api/push/test/dev_me": () => new Response(null, { status: 204 }),
    "DELETE /api/push/subscriptions/dev_phone": () => new Response(null, { status: 204 }),
  });
  const screen = await render(<NotificationsRow Row={Row} />);
  await expect.element(screen.getByTestId("push-state")).toHaveTextContent("Off on this device.");
  expect(ask).not.toHaveBeenCalled();
  await expect.element(screen.getByLabelText("What a notification looks like")).toBeVisible();

  await screen.getByRole("button", { name: "Turn on for this device" }).click();
  await expect.element(screen.getByTestId("push-state")).toHaveTextContent("On for this device, Chrome on Mac.");
  expect(ask).toHaveBeenCalledOnce();
  expect(subscribe).toHaveBeenCalledWith(expect.objectContaining({ userVisibleOnly: true }));

  const notices = screen.getByRole("checkbox", { name: /Needs-you notices/ });
  await expect.element(notices).toBeChecked();
  await notices.click();
  await expect.element(notices).not.toBeChecked();
  await expect.element(screen.getByText("Saved.")).toBeVisible();

  await screen.getByRole("button", { name: "Send a test" }).click();
  await expect.element(screen.getByText("Sent. It should arrive within a few seconds.")).toBeVisible();

  const others = screen.getByRole("list", { name: "Other devices" });
  await expect.element(others).toMatchTextContent(/Chrome on Android/);
  await expect.element(others).not.toMatchTextContent(/Chrome on Mac/);
  await screen.getByRole("button", { name: "Remove Chrome on Android" }).click();
  expect(await answerConfirm(screen, "Remove")).toContain("Remove Chrome on Android?");
  await expect.element(screen.getByRole("list", { name: "Other devices" })).not.toBeInTheDocument();
  expect(calls).toContain("DELETE /api/push/subscriptions/dev_phone");
});

test("a blocked browser, and an unsupported one, say what to do and offer no button", async () => {
  browser("denied");
  fakeFetch({ "GET /api/push/subscriptions": () => Response.json([]) });
  const screen = await render(<NotificationsRow Row={Row} />);
  await expect.element(screen.getByTestId("push-state")).toMatchTextContent(/Blocked by the browser/);
  expect(screen.getByRole("button", { name: "Turn on for this device" }).elements()).toHaveLength(0);
});

test("a failed test says why", async () => {
  browser("granted", true);
  fakeFetch({
    "GET /api/push/subscriptions": () => Response.json([device("dev_me", "Chrome on Mac")]),
    "POST /api/push/subscriptions": () => Response.json(device("dev_me", "Chrome on Mac")),
    "POST /api/push/test/dev_me": () => Response.json({ detail: "the push service did not take it (HTTP 403)" }, { status: 502 }),
  });
  const screen = await render(<NotificationsRow Row={Row} />);
  await screen.getByRole("button", { name: "Send a test" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("the push service did not take it (HTTP 403)");
});

test("the prompt waits for an answered gate, asks only after Turn on, and Not now snoozes it", async () => {
  const { ask } = browser("default");
  fakeFetch({});
  const screen = await render(<PushPrompt hidden={false} />);
  expect(screen.getByRole("complementary").elements()).toHaveLength(0);

  markAnswered();
  const card = screen.getByRole("complementary", { name: "Turn on notifications" });
  await expect.element(card).toMatchTextContent(/Get a nudge when a run needs you next\?/);
  expect(ask).not.toHaveBeenCalled();
  await card.getByRole("button", { name: "Not now" }).click();
  await expect.element(card).not.toBeInTheDocument();
  expect(snoozed()).toBe(true);
  expect(snoozed(Date.now() + 31 * 24 * 3600 * 1000)).toBe(false);
});

test("Turn on in the prompt asks the browser and stores the device", async () => {
  const { ask } = browser("default");
  const calls = fakeFetch({
    "GET /api/push/key": () => Response.json({ public_key: P256DH }),
    "POST /api/push/subscriptions": () => Response.json(device("dev_me", "Chrome on Mac")),
  });
  setPwa({ answeredNow: true });
  const screen = await render(<PushPrompt hidden={false} />);
  await screen.getByRole("button", { name: "Turn on" }).click();
  await expect.element(screen.getByText("Notifications are on for this device.")).toBeVisible();
  expect(ask).toHaveBeenCalledOnce();
  expect(calls).toContain("POST /api/push/subscriptions");
});

test("the prompt stays away while the install hint is up", async () => {
  browser("default");
  setPwa({ answeredNow: true });
  const screen = await render(<PushPrompt hidden />);
  await new Promise((r) => setTimeout(r, 50));
  expect(screen.getByRole("complementary").elements()).toHaveLength(0);
});

test("devices are named by browser and system", () => {
  expect(deviceLabel("Mozilla/5.0 (Linux; Android 14) AppleWebKit Chrome/131 Mobile Safari")).toBe("Chrome on Android");
  expect(deviceLabel("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Version/18 Mobile Safari")).toBe("Safari on iPhone");
  expect(deviceLabel("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) Firefox/133")).toBe("Firefox on Mac");
  expect(deviceLabel("curl")).toBe("A browser");
});
