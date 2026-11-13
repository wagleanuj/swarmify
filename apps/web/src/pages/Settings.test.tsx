import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { answerConfirm, mountAt } from "../test/mount";

const video = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00" };
const shown = "https://hooks.slack.com/...abcd";

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  localStorage.clear();
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
  await expect.element(screen.getByText("Saved. Send a test message to check it reaches Slack.")).toBeVisible(); // a toast
  expect(bodies[0]).toEqual({ slack_webhook: "https://hooks.slack.com/services/T0/B0/abcd" });

  await screen.getByRole("button", { name: "Send a test message" }).click();
  await expect.element(screen.getByText("Test sent. It should be in Slack now.")).toBeVisible();
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
  await expect.element(screen.getByText("Not saved: the webhook must be an http or https URL.")).toBeVisible();
  await expect.element(screen.getByTestId("slack-state")).toHaveTextContent(`Posting to ${shown}`);
});

const models = { default: "claude-opus-5-5", models: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"] };

test("one Save shows only once something changed, sends the World and Runs rows together, and a toast confirms it", async () => {
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
  const form = screen.getByRole("form", { name: "World settings" });
  const save = screen.getByRole("button", { name: "Save", exact: true });
  await expect.element(save).not.toBeInTheDocument();

  await form.getByRole("spinbutton", { name: "Monthly budget, dollars" }).fill("25");
  await expect.element(save).toBeVisible();
  // Back to what the world has: nothing to save, so the bar goes again.
  await form.getByRole("spinbutton", { name: "Monthly budget, dollars" }).fill("");
  await expect.element(save).not.toBeInTheDocument();

  await form.getByRole("spinbutton", { name: "Monthly budget, dollars" }).fill("25");
  await form.getByRole("spinbutton", { name: "Keep videos and images for, days" }).fill("30");
  const model = form.getByRole("combobox", { name: "Model" });
  await expect.element(model).toHaveDisplayValue("Default: claude-opus-5-5");
  await model.selectOptions("claude-sonnet-5-5");
  await form.getByRole("spinbutton", { name: "Run cap" }).fill("1");
  await form.getByRole("spinbutton", { name: "Workers in the cast" }).fill("2");
  expect(document.querySelectorAll("main button.primary")).toHaveLength(1);
  await save.click();
  await expect.element(screen.getByText("Saved. Applies to the next run that starts.")).toBeVisible(); // a toast
  expect(bodies).toEqual([{ monthly_budget_usd: 25, retention_days: 30, model: "claude-sonnet-5-5", run_cap: 1, agent_count: 2 }]);
  await expect.element(screen.getByTestId("spent")).toHaveTextContent("Spent this month: $1.50, an estimate of $25.00.");
  await expect.element(save).not.toBeInTheDocument();
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

test("a refused setting is shown as the API's reason, and the change stays to fix", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/notifications": () => Response.json({ slack_webhook: null }),
    "PATCH /api/worlds/world_1": () =>
      Response.json({ detail: "the run cap is 1 to 8, got 9" }, { status: 422 }),
  });
  const { screen } = await mountAt("/worlds/world_1/settings");
  const form = screen.getByRole("form", { name: "World settings" });
  await form.getByRole("spinbutton", { name: "Run cap" }).fill("8");
  await screen.getByRole("button", { name: "Save", exact: true }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("Not saved: the run cap is 1 to 8, got 9.");
  await expect.element(screen.getByRole("button", { name: "Save", exact: true })).toBeVisible();
});

test("delete sits last, says what goes and asks by the world's name before deleting", async () => {
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/notifications": () => Response.json({ slack_webhook: null }),
    "DELETE /api/worlds/world_1": () => new Response(null, { status: 204 }),
    "GET /api/worlds": () => Response.json([]),
  });
  const { screen, router } = await mountAt("/worlds/world_1/settings");
  const rows = screen.getByRole("main").getByRole("region");
  const danger = rows.last();
  await expect.element(danger).toHaveAccessibleName("Danger zone");
  await expect.element(danger).toMatchTextContent(/removes its folder, runs, library and memory/);
  const remove = danger.getByRole("button", { name: "Delete Video" });

  await remove.click();
  expect(await answerConfirm(screen, "Cancel")).toMatch(/^Delete Video\?.*cannot be undone/);
  expect(calls).not.toContain("DELETE /api/worlds/world_1");

  await remove.click();
  await answerConfirm(screen, "Delete world");
  await expect.element(screen.getByRole("heading", { name: "Your worlds" })).toBeVisible();
  expect(calls).toContain("DELETE /api/worlds/world_1");
  expect(router.state.location.pathname).toBe("/");
});

test("role names are neutral until the Pied Piper skin is turned on, in this browser only", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/notifications": () => Response.json({ slack_webhook: null }),
  });
  const { screen } = await mountAt("/worlds/world_1/settings");
  const names = screen.getByRole("checkbox", { name: "Pied Piper names" });
  await expect.element(names).not.toBeChecked();
  await expect.element(screen.getByText(/The planner and the judge always come along/)).toBeVisible();

  await names.click();
  await expect.element(screen.getByText(/Richard and Erlich always come along/)).toBeVisible();
  expect(localStorage.getItem("swarmify.skin")).toBe("pied-piper");
  await names.click();
  await expect.element(screen.getByText(/The planner and the judge always come along/)).toBeVisible();
  expect(localStorage.getItem("swarmify.skin")).toBeNull();
});

test("the world's panels wait for the world, so a slow load cannot wipe what was typed", async () => {
  let loaded!: () => void;
  const ready = new Promise<void>((resolve) => (loaded = resolve));
  fakeFetch({
    "GET /api/worlds/world_1": async () => {
      await ready;
      return Response.json({ ...video, monthly_budget_usd: null, retention_days: 90, run_cap: 2, agent_count: 4 });
    },
    "GET /api/worlds/world_1/notifications": () => Response.json({ slack_webhook: null }),
  });
  const { screen } = await mountAt("/worlds/world_1/settings");
  const budget = screen.getByRole("spinbutton", { name: "Monthly budget, dollars" });
  await expect.element(budget).toBeDisabled();
  await expect.element(screen.getByRole("spinbutton", { name: "Run cap" })).toBeDisabled();
  loaded();
  await expect.element(budget).toBeEnabled();
  await budget.fill("50");
  await expect.element(budget).toHaveValue(50);
});

test("while the world loads, its line, spend and Slack state are skeletons, not Loading text", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => new Promise<Response>(() => undefined),
    "GET /api/worlds/world_1/notifications": () => new Promise<Response>(() => undefined),
    "GET /api/tokens": () => new Promise<Response>(() => undefined),
  });
  const { screen } = await mountAt("/worlds/world_1/settings");
  await expect.element(screen.getByRole("heading", { name: "Settings", level: 1 })).toBeVisible();
  expect(screen.container.querySelector("[aria-busy='true']")).not.toBeNull();
  expect(screen.getByTestId("slack-state").element().querySelector(".sk")).not.toBeNull();
  expect(screen.getByTestId("spent").element().querySelector(".sk")).not.toBeNull();
  expect(screen.container.querySelectorAll(".token-list .sk")).toHaveLength(2);
  expect(screen.container.textContent).not.toMatch(/Loading\.\.\.|Loading \w/);
  // number pads on a phone
  await expect.element(screen.getByRole("spinbutton", { name: "Monthly budget, dollars" })).toHaveAttribute("inputmode", "decimal");
  await expect.element(screen.getByRole("spinbutton", { name: "Run cap" })).toHaveAttribute("inputmode", "numeric");
});
