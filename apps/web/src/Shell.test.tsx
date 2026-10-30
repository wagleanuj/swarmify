import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page, userEvent } from "vitest/browser";

import "./index.css";
import { FakeEventSource, fakeFetch, gate, run } from "./test/fakes";
import { mountAt } from "./test/mount";

const ts = "2026-10-26T10:00:00+00:00";
const worlds = [
  { id: "world_1", name: "Video", purpose: "", created_at: ts },
  { id: "world_2", name: "Podcast", purpose: "", created_at: ts },
];

beforeEach(async () => {
  FakeEventSource.install();
  await page.viewport(1280, 800);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("the sidebar marks the current screen and counts open gates live from the stream", async () => {
  let open = [gate("pick", { run_id: "run_1", stage_id: "s1" }), gate("approve", { run_id: "run_2", stage_id: "s2" })];
  fakeFetch({
    "GET /api/worlds": () => Response.json(worlds),
    "GET /api/gates": () => Response.json(open),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs");
  const nav = screen.getByRole("navigation", { name: "Main" });
  await expect.element(nav.getByRole("link", { name: /^Needs you/ })).toHaveAccessibleName("Needs you 2 waiting");
  await expect.element(nav.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
  await expect.element(nav.getByRole("link", { name: "Overview" })).not.toHaveAttribute("aria-current");

  open = open.slice(1);
  FakeEventSource.emit("run.updated", { payload: run("running", []) });
  await expect.element(nav.getByRole("link", { name: /^Needs you/ })).toHaveAccessibleName("Needs you 1 waiting");
});

test("answering in Needs you drops the count at once", async () => {
  fakeFetch({
    "GET /api/worlds": () => Response.json(worlds),
    "GET /api/gates": () => Response.json([gate("confirm")]),
    "POST /api/runs/run_1/decision": () => Response.json(run("running", [])),
  });
  const { screen } = await mountAt("/gates");
  const nav = screen.getByRole("navigation", { name: "Main" });
  await expect.element(nav.getByRole("link", { name: /^Needs you/ })).toHaveAccessibleName("Needs you 1 waiting");
  await expect.element(nav.getByRole("link", { name: /^Needs you/ })).toHaveAttribute("aria-current", "page");
  await screen.getByRole("button", { name: "Confirm" }).click();
  await expect.element(nav.getByRole("link", { name: /^Needs you/ })).toHaveAccessibleName("Needs you");
});

test("the world switcher names the route's world, and a global route keeps it", async () => {
  fakeFetch({ "GET /api/worlds": () => Response.json(worlds), "GET /api/gates": () => Response.json([]) });
  const { router, screen } = await mountAt("/worlds/world_2/settings");
  const nav = screen.getByRole("navigation", { name: "Main" });
  await expect.element(nav.getByTestId("world-switcher")).toHaveTextContent("WorldPodcast");
  await expect.element(nav.getByTestId("world-switcher")).toHaveAttribute("href", "/");
  await expect.element(nav.getByRole("link", { name: "Settings" })).toHaveAttribute("aria-current", "page");

  await router.navigate({ to: "/evals" });
  await expect.element(nav.getByRole("link", { name: "Evals" })).toHaveAttribute("aria-current", "page");
  await expect.element(nav.getByTestId("world-switcher")).toHaveTextContent("WorldPodcast");
  await expect.element(nav.getByRole("link", { name: "Runs" })).toHaveAttribute("href", "/worlds/world_2/runs");
});

test("on a narrow screen the sidebar folds behind a menu button into a full-screen sheet", async () => {
  await page.viewport(800, 844);
  fakeFetch({ "GET /api/worlds": () => Response.json(worlds), "GET /api/gates": () => Response.json([]) });
  const { screen } = await mountAt("/worlds/world_1/cast");
  const nav = screen.getByRole("navigation", { name: "Main", includeHidden: true });
  await expect.element(screen.getByRole("heading", { level: 1 })).toBeVisible();
  await expect.element(nav).not.toBeVisible();
  await screen.getByRole("button", { name: "Menu" }).click();
  await expect.element(nav).toBeVisible();
  await nav.getByRole("link", { name: "Library" }).click();
  await expect.element(nav).not.toBeVisible();
  await screen.getByRole("button", { name: "Menu" }).click();
  await nav.getByRole("button", { name: "Close menu" }).click();
  await expect.element(nav).not.toBeVisible();
});

test("at desktop width there is no tab bar or world chip", async () => {
  fakeFetch({ "GET /api/worlds": () => Response.json(worlds), "GET /api/gates": () => Response.json([]) });
  const { screen } = await mountAt("/worlds/world_1/cast");
  await expect.element(screen.getByRole("navigation", { name: "Main" })).toBeVisible();
  await expect.element(screen.getByRole("navigation", { name: "Tabs", includeHidden: true })).not.toBeVisible();
  await expect.element(screen.getByRole("button", { name: "Video", includeHidden: true })).not.toBeVisible();
});

test("on a phone five thumb-sized tabs replace the menu, with the open decisions on Needs you", async () => {
  await page.viewport(390, 844);
  fakeFetch({
    "GET /api/worlds": () => Response.json(worlds),
    "GET /api/gates": () => Response.json([gate("pick"), gate("approve", { stage_id: "s2" })]),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  const tabs = screen.getByRole("navigation", { name: "Tabs" });
  await expect.element(tabs.getByRole("link", { name: /^Needs you/ })).toHaveAccessibleName("Needs you 2 waiting");
  await expect.element(screen.getByRole("navigation", { name: "Main", includeHidden: true })).not.toBeVisible();
  await expect.element(screen.getByRole("button", { name: "Menu", includeHidden: true })).not.toBeVisible();
  expect(tabs.getByRole("link").elements().map((a) => a.textContent)).toEqual(["Needs you2", "Runs", "Workflows", "Library"]);
  await expect.element(tabs.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
  await expect.element(tabs.getByRole("link", { name: "Runs" })).toHaveClass("on");
  await expect.element(tabs.getByRole("button", { name: "More" })).not.toHaveClass("on");
  for (const t of tabs.getByRole("link").elements()) expect(t.getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
  expect(tabs.element().getBoundingClientRect().bottom).toBe(844);
});

test("on a phone the world chip opens a sheet of every world and what each waits on", async () => {
  await page.viewport(390, 844);
  fakeFetch({
    "GET /api/worlds": () => Response.json(worlds),
    "GET /api/gates": () => Response.json([gate("pick", { world_id: "world_2" })]),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
    "GET /api/worlds/world_2/runs": () => Response.json([]),
  });
  const { router, screen } = await mountAt("/worlds/world_1/runs");
  const chip = screen.getByRole("button", { name: "Video" });
  await chip.click();
  const sheet = screen.getByRole("dialog", { name: "Worlds", includeHidden: true });
  await expect.element(sheet).toBeVisible();
  await expect.element(sheet.getByRole("link", { name: /^Video/ })).toHaveTextContent("Videonothing waiting");
  await expect.element(sheet.getByRole("link", { name: /^Podcast/ })).toHaveTextContent("Podcast1 waiting");
  await expect.element(sheet.getByRole("link", { name: /^All worlds/ })).toHaveTextContent("All worlds2 worlds");
  await userEvent.keyboard("{Escape}");
  await expect.element(sheet).not.toBeVisible();

  await chip.click();
  await sheet.getByRole("link", { name: /^Podcast/ }).click();
  await expect.element(sheet).not.toBeVisible();
  expect(router.state.location.pathname).toBe("/worlds/world_2/runs");
  await expect.element(screen.getByRole("button", { name: "Podcast" })).toBeVisible();
});

test("on a phone More lists every other page with a one-line summary, and the backdrop closes it", async () => {
  await page.viewport(390, 844);
  const today = new Date().toISOString();
  const connector = { missing: [], sign_in: [], kind: "tool", command: ["x"], url: null, name: "x" };
  fakeFetch({
    "GET /api/worlds": () => Response.json(worlds),
    "GET /api/gates": () => Response.json([]),
    "GET /api/worlds/world_1/runs": () => Response.json([run("done", [], { created_at: today, spent_usd: 8.61 })]),
    "GET /api/worlds/world_1/cast": () => Response.json([{}, {}, {}, {}]),
    "GET /api/worlds/world_1/connectors": () => Response.json([connector, connector, { ...connector, missing: ["KEY"] }]),
    "GET /api/worlds/world_1/library": () => Response.json([]),
    "GET /api/worlds/world_1/assets": () => Response.json([{}, {}]),
  });
  const { router, screen } = await mountAt("/worlds/world_1/library");
  const more = screen.getByRole("navigation", { name: "Tabs" }).getByRole("button", { name: "More" });
  await more.click();
  const sheet = screen.getByRole("dialog", { name: "More", includeHidden: true });
  await expect.element(sheet.getByRole("link", { name: /^Overview/ })).toHaveTextContent("Overview1 run today · $8.61");
  const rows = sheet.getByRole("link").elements().map((a) => a.textContent);
  expect(rows).toEqual([
    "Overview1 run today · $8.61",
    "Assets2 files",
    "Memorygraph and notes",
    "Cast4 agents",
    "Connectors2 of 3 ready",
    "Evalsscorecards",
    "Settingsthis world, keys",
    "All worlds2 worlds",
  ]);
  await expect.element(more).toHaveClass("on");

  // A tap above the sheet lands on the dialog's backdrop.
  await userEvent.click(sheet, { position: { x: 195, y: -40 } });
  await expect.element(sheet).not.toBeVisible();
  await more.click();
  await sheet.getByRole("link", { name: /^All worlds/ }).click();
  await expect.element(sheet).not.toBeVisible();
  expect(router.state.location.pathname).toBe("/");
  await expect.element(more).toHaveClass("on");
});
