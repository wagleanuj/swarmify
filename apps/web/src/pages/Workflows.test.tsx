import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, run } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:00:00+00:00";
const workflows = [
  { id: "wf_1", world_id: "world_1", name: "Weekly short", current_version: 3, created_at: ts },
  { id: "wf_2", world_id: "world_1", name: "Daily digest", current_version: 1, created_at: ts },
];
const runs = [
  run("done", [], { id: "run_1", workflow_id: "wf_1", spent_usd: 4 }),
  run("done", [], { id: "run_2", workflow_id: "wf_1", spent_usd: 2.5 }),
  run("done", [], { id: "run_3", workflow_id: null, spent_usd: 9 }),
];

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("the sidebar's Workflows lists each with its version, runs and average cost, and starts a run", async () => {
  let started: unknown = null;
  fakeFetch({
    "GET /api/worlds": () => Response.json([{ id: "world_1", name: "Video", purpose: "", created_at: ts }]),
    "GET /api/gates": () => Response.json([]),
    "GET /api/worlds/world_1/workflows": () => Response.json(workflows),
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
    "POST /api/workflows/wf_1/runs": async (req) => {
      started = await req.json();
      return Response.json(run("queued", [], { id: "run_9" }), { status: 201 });
    },
    "GET /api/runs/run_9": () => Response.json(run("queued", [], { id: "run_9" })),
  });
  const { screen, router } = await mountAt("/worlds/world_1/workflows");
  const nav = screen.getByRole("navigation", { name: "Main" });
  await expect.element(nav.getByRole("link", { name: "Workflows" })).toHaveAttribute("href", "/worlds/world_1/workflows");
  await expect.element(nav.getByRole("link", { name: "Workflows" })).toHaveAttribute("aria-current", "page");
  await expect.element(screen.getByRole("heading", { name: "Workflows", level: 1 })).toBeVisible();
  const rows = screen.getByTestId("workflow");
  await expect.element(rows.first()).toMatchTextContent(/Weekly short.*v3.*2.*\$3\.25.*Start run/);
  await expect.element(rows.nth(1)).toMatchTextContent(/Daily digest.*v1.*0.*-/);
  await expect.element(rows.first().getByRole("link")).toHaveAttribute("href", "/worlds/world_1/workflows/wf_1");
  await expect
    .element(screen.getByRole("link", { name: "New workflow" }))
    .toHaveAttribute("href", "/worlds/world_1/workflows/new");

  await screen.getByRole("button", { name: "Start a run of Weekly short" }).click();
  await expect.poll(() => router.state.location.pathname).toBe("/worlds/world_1/runs/run_9");
  expect(started).toEqual({ baseline: false });
});
