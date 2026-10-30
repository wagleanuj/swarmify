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

const models = { default: "claude-opus-5-5", models: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"] };

test("each panel saves only its own settings, stays on the page and stamps the save", async () => {
  const bodies: unknown[] = [];
  const world = { ...video, retention_days: 90, monthly_budget_usd: null, run_cap: 2, agent_count: 4, model: null };
  fakeFetch({
    "GET /api/models": () => Response.json(models),
    "GET /api/worlds/world_1": () => Response.json(world),
    "GET /api/worlds/world_1/spend": () =>
      Response.json({ month: "2026-10", spent_usd: 1.5, monthly_budget_usd: null }),
    "GET /api/worlds/world_1/notifications": () => Response.json({ slack_webhook: null }),
    "PATCH /api/worlds/world_1": async (req) => {
      const body = (await req.json()) as Record<string, unknown>;
      bodies.push(body);
      return Response.json({ ...world, ...body });
    },
  });
  const { screen, router } = await mountAt("/worlds/world_1/settings");
  await expect.element(screen.getByTestId("spent")).toHaveTextContent("Spent this month: $1.50, an estimate.");
  const worldPanel = screen.getByRole("form", { name: "World" });
  await worldPanel.getByRole("spinbutton", { name: "Monthly budget, dollars" }).fill("25");
  await worldPanel.getByRole("spinbutton", { name: "Keep videos and images for, days" }).fill("30");
  const model = worldPanel.getByRole("combobox", { name: "Model" });
  await expect.element(model).toHaveDisplayValue("Default: claude-opus-5-5");
  await model.selectOptions("claude-sonnet-5-5");
  await worldPanel.getByRole("button", { name: "Save" }).click();
  await expect.element(worldPanel.getByRole("status")).toMatchTextContent(/^Saved at \d/);
  expect(bodies).toEqual([{ monthly_budget_usd: 25, retention_days: 30, model: "claude-sonnet-5-5" }]);
  await expect.element(screen.getByTestId("spent")).toHaveTextContent("Spent this month: $1.50, an estimate of $25.00.");

  const runs = screen.getByRole("form", { name: "Runs at once" });
  await runs.getByRole("spinbutton", { name: "Run cap" }).fill("1");
  await runs.getByRole("spinbutton", { name: "Workers in the cast" }).fill("2");
  await runs.getByRole("button", { name: "Save" }).click();
  await expect.element(runs.getByRole("status")).toMatchTextContent(/^Saved at \d/);
  expect(bodies[1]).toEqual({ run_cap: 1, agent_count: 2 });
  expect(router.state.location.pathname).toBe("/worlds/world_1/settings");
});

test("the run cap says a run waiting at a gate holds a slot", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/notifications": () => Response.json({ slack_webhook: null }),
  });
  const { screen } = await mountAt("/worlds/world_1/settings");
  await expect
    .element(screen.getByRole("region", { name: "Runs at once" }))
    .toMatchTextContent(/A run waiting at a gate keeps its slot/);
  await expect.element(screen.getByTestId("run-cap-help")).toMatchTextContent(/waiting at a gate hold a slot/);
});

test("a refused setting is shown as the API's reason in its own panel", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/notifications": () => Response.json({ slack_webhook: null }),
    "PATCH /api/worlds/world_1": () =>
      Response.json({ detail: "the run cap is 1 to 8, got 9" }, { status: 422 }),
  });
  const { screen } = await mountAt("/worlds/world_1/settings");
  const runs = screen.getByRole("form", { name: "Runs at once" });
  await runs.getByRole("spinbutton", { name: "Run cap" }).fill("8");
  await runs.getByRole("button", { name: "Save" }).click();
  await expect.element(runs.getByRole("alert")).toHaveTextContent("Not saved: the run cap is 1 to 8, got 9.");
});

test("delete sits last, says what goes and asks by the world's name before deleting", async () => {
  const asked: string[] = [];
  let answer = false;
  vi.spyOn(window, "confirm").mockImplementation((q) => {
    asked.push(String(q));
    return answer;
  });
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/notifications": () => Response.json({ slack_webhook: null }),
    "DELETE /api/worlds/world_1": () => new Response(null, { status: 204 }),
    "GET /api/worlds": () => Response.json([]),
  });
  const { screen, router } = await mountAt("/worlds/world_1/settings");
  const rows = screen.getByRole("main").getByRole("region");
  const danger = rows.last();
  await expect.element(danger).toHaveAccessibleName("Danger");
  await expect.element(danger).toMatchTextContent(/removes its folder, runs, library and memory/);
  const remove = danger.getByRole("button", { name: "Delete Video" });

  await remove.click();
  expect(asked[0]).toMatch(/^Delete Video\? .*cannot be undone/);
  expect(calls).not.toContain("DELETE /api/worlds/world_1");

  answer = true;
  await remove.click();
  await expect.element(screen.getByRole("heading", { name: "Your worlds" })).toBeVisible();
  expect(calls).toContain("DELETE /api/worlds/world_1");
  expect(router.state.location.pathname).toBe("/");
});
