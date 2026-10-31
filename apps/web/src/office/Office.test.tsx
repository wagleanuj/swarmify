import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, run } from "../test/fakes";
import { mountAt } from "../test/mount";

const video = {
  id: "world_1",
  name: "Video",
  purpose: "Weekly shorts.",
  created_at: "2026-10-26T10:00:00+00:00",
  run_cap: 3,
  skin: { view: "office" },
};
const spend = { month: "2026-10", spent_usd: 3.5, monthly_budget_usd: 20 };

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** Answers the world, its spend and runs, and records each settings write. */
function office(world: object = video, runs = [run("running", [])]) {
  const saved: unknown[] = [];
  const calls = fakeFetch({
    "GET /api/worlds": () => Response.json([world]),
    "GET /api/worlds/world_1": () => Response.json(world),
    "GET /api/worlds/world_1/spend": () => Response.json(spend),
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
    "GET /api/worlds/world_1/workflows": () => Response.json([]),
    "PATCH /api/worlds/world_1": async (req) => {
      const body = (await req.json()) as { skin: { view: string } };
      saved.push(body);
      return Response.json({ ...world, ...body });
    },
  });
  return { saved, calls };
}

test("the office has its own shell: a top bar of figures beside their reference, no sidebar", async () => {
  office();
  const { screen } = await mountAt("/worlds/world_1/office");
  await expect.element(screen.getByRole("heading", { name: "Video", level: 1 })).toBeVisible();
  await expect.element(screen.getByTestId("office-spend")).toHaveTextContent("$3.50 of $20.00 this month");
  await expect.element(screen.getByTestId("office-runs")).toHaveTextContent("1 of 3 runs going");
  await expect.element(screen.getByRole("region", { name: "The office" })).toBeVisible();
  expect(screen.getByRole("navigation", { name: "Main" }).elements()).toHaveLength(0);
  expect(screen.getByRole("navigation", { name: "Tabs" }).elements()).toHaveLength(0);

  // A run that starts holds a slot; a child run spends its parent's.
  FakeEventSource.emit("run.updated", { payload: run("waiting_gate", [], { id: "run_2" }) });
  FakeEventSource.emit("run.updated", { payload: run("running", [], { id: "run_3", parent_run_id: "run_1" }) });
  await expect.element(screen.getByTestId("office-runs")).toHaveTextContent("2 of 3 runs going");
});

test("a world with no budget says so instead of a bare figure", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/spend": () => Response.json({ ...spend, spent_usd: 0, monthly_budget_usd: null }),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/office");
  await expect.element(screen.getByTestId("office-spend")).toHaveTextContent("$0.00 spent, no monthly budget");
  await expect.element(screen.getByTestId("office-runs")).toHaveTextContent("0 of 3 runs going");
});

test("Plain view saves the setting and opens the plain overview in the sidebar shell", async () => {
  const { saved } = office();
  const { screen, router } = await mountAt("/worlds/world_1/office");
  await screen.getByRole("button", { name: "Plain view" }).click();
  await expect.element(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  expect(saved).toEqual([{ skin: { view: "plain" } }]);
  expect(router.state.location.pathname).toBe("/worlds/world_1");
  await expect.element(screen.getByRole("button", { name: "Office" })).toBeVisible();
});

test("Office on the plain overview saves the setting and opens the office", async () => {
  const { saved } = office({ ...video, skin: { view: "plain" } });
  const { screen, router } = await mountAt("/worlds/world_1");
  await screen.getByRole("button", { name: "Office" }).click();
  await expect.element(screen.getByRole("button", { name: "Plain view" })).toBeVisible();
  expect(saved).toEqual([{ skin: { view: "office" } }]);
  expect(router.state.location.pathname).toBe("/worlds/world_1/office");
  expect(screen.getByRole("navigation", { name: "Main" }).elements()).toHaveLength(0);
});

test("a refused switch stays put and says so", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "PATCH /api/worlds/world_1": () => Response.json({ detail: "no" }, { status: 422 }),
  });
  const { screen, router } = await mountAt("/worlds/world_1/office");
  await screen.getByRole("button", { name: "Plain view" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("Could not switch to the plain view. Try again.");
  expect(router.state.location.pathname).toBe("/worlds/world_1/office");
});

test("the world links open each world in its chosen view", async () => {
  const plain = { ...video, id: "world_2", name: "Research", skin: { view: "plain" } };
  fakeFetch({ "GET /api/worlds": () => Response.json([video, plain]) });
  const { screen } = await mountAt("/");
  const worlds = screen.getByRole("region", { name: "Worlds" });
  await expect.element(worlds.getByRole("link", { name: "Video" })).toHaveAttribute("href", "/worlds/world_1/office");
  await expect.element(worlds.getByRole("link", { name: "Research" })).toHaveAttribute("href", "/worlds/world_2");
  // The sidebar's world link follows the current world's view too.
  await expect
    .element(screen.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Overview" }))
    .toHaveAttribute("href", "/worlds/world_1/office");
});

test("an unknown world says so in the office", async () => {
  fakeFetch({});
  const { screen } = await mountAt("/worlds/world_1/office");
  await expect.element(screen.getByRole("alert")).toHaveTextContent("No such world. Back to your worlds.");
});
