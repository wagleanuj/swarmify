import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";

const video = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00" };
const shown = "https://hooks.slack.com/...abcd";

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("saves a webhook, sends a test, then disconnects", async () => {
  const bodies: unknown[] = [];
  let stored: string | null = null;
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/notifications": () => Response.json({ slack_webhook: stored }),
    "PUT /api/worlds/world_1/notifications": async (req) => {
      const body = (await req.json()) as { slack_webhook: string };
      bodies.push(body);
      stored = body.slack_webhook ? shown : null;
      return Response.json({ slack_webhook: stored });
    },
    "POST /api/worlds/world_1/notifications/test": () => new Response(null, { status: 204 }),
  });
  const { screen } = await mountAt("/worlds/world_1/settings");
  await expect.element(screen.getByTestId("slack-state")).toMatchTextContent(/Not connected/);
  expect(screen.getByRole("button", { name: "Send a test message" }).elements()).toHaveLength(0);

  await screen
    .getByRole("textbox", { name: "Incoming webhook URL" })
    .fill("https://hooks.slack.com/services/T0/B0/abcd");
  await screen.getByRole("button", { name: "Save webhook" }).click();
  await expect.element(screen.getByTestId("slack-state")).toHaveTextContent(`Posting to ${shown}`);
  await expect.element(screen.getByRole("status")).toMatchTextContent(/Saved/);
  expect(bodies[0]).toEqual({ slack_webhook: "https://hooks.slack.com/services/T0/B0/abcd" });

  await screen.getByRole("button", { name: "Send a test message" }).click();
  await expect.element(screen.getByRole("status")).toMatchTextContent(/Test sent/);
  expect(calls).toContain("POST /api/worlds/world_1/notifications/test");

  await screen.getByRole("button", { name: "Disconnect" }).click();
  await expect.element(screen.getByTestId("slack-state")).toMatchTextContent(/Not connected/);
  expect(bodies[1]).toEqual({ slack_webhook: "" });
});

test("shows why Slack refused the test and why a webhook was rejected", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/notifications": () => Response.json({ slack_webhook: shown }),
    "PUT /api/worlds/world_1/notifications": () =>
      Response.json({ detail: "the webhook must be an http or https URL" }, { status: 422 }),
    "POST /api/worlds/world_1/notifications/test": () =>
      Response.json({ detail: "Slack did not take the message: HTTP Error 404" }, { status: 502 }),
  });
  const { screen } = await mountAt("/worlds/world_1/settings");
  await screen.getByRole("button", { name: "Send a test message" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent(
    "Slack did not take the message: HTTP Error 404.",
  );
  await screen
    .getByRole("textbox", { name: "Replace the incoming webhook URL" })
    .fill("http://x.test/hook");
  await screen.getByRole("button", { name: "Save webhook" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent(
    "Not saved: the webhook must be an http or https URL.",
  );
  await expect.element(screen.getByTestId("slack-state")).toHaveTextContent(`Posting to ${shown}`);
});
