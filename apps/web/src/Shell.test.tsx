import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

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

test("on a phone the sidebar folds behind a menu button into a full-screen sheet", async () => {
  await page.viewport(390, 844);
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
