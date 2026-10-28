import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";
import { collapse } from "./WorldOverview";

const video = {
  id: "world_1",
  name: "Video",
  purpose: "Weekly shorts.",
  created_at: "2026-10-26T10:00:00+00:00",
};
const health = () => Response.json({ version: "0.1.0" });

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("a run of the same event type collapses into one activity row with a count", () => {
  const at = (seq: number, type: string) => ({ seq, type, ts: video.created_at });
  const rows = collapse([at(1, "run.updated"), at(2, "run.updated"), at(3, "run.ended"), at(4, "run.updated")]);
  expect(rows.map((r) => [r.seq, r.type, r.times])).toEqual([
    [2, "run.updated", 2],
    [3, "run.ended", 1],
    [4, "run.updated", 1],
  ]);
});

test("shows the world, its tiles and live activity from the world's event stream", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1");
  await expect.element(screen.getByRole("heading", { name: "Video", level: 1 })).toBeVisible();
  await expect.element(screen.getByText("Weekly shorts.")).toBeVisible();
  await expect.element(screen.getByText("Needs you")).toBeVisible();
  await expect.element(screen.getByRole("link", { name: "worlds" })).toHaveAttribute("href", "/");
  await expect.element(screen.getByTestId("no-workflows")).toMatchTextContent(/recipe a run follows/);
  await expect
    .element(screen.getByRole("link", { name: "New workflow" }))
    .toHaveAttribute("href", "/worlds/world_1/workflows/new");
  await expect
    .element(screen.getByRole("link", { name: "Manage connectors" }))
    .toHaveAttribute("href", "/worlds/world_1/connectors");
  await expect
    .element(screen.getByRole("link", { name: "Open settings" }))
    .toHaveAttribute("href", "/worlds/world_1/settings");

  expect(FakeEventSource.open.map((s) => s.url)).toEqual(["/api/worlds/world_1/events"]);
  FakeEventSource.emit("world.created", { seq: 1, type: "world.created", ts: video.created_at });
  await expect.element(screen.getByTestId("activity")).toMatchTextContent(/World created/);
});

test("a replayed burst of run updates refetches the gates once, not once per event", async () => {
  const calls = fakeFetch({ "GET /api/worlds/world_1": () => Response.json(video) });
  const { screen } = await mountAt("/worlds/world_1");
  await expect.element(screen.getByRole("heading", { name: "Video", level: 1 })).toBeVisible();
  await vi.waitFor(() => expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(1));
  for (let seq = 1; seq <= 50; seq++) {
    const payload = { id: `run_${seq}` };
    FakeEventSource.emit("run.updated", { seq, type: "run.updated", ts: video.created_at, payload });
  }
  await expect.element(screen.getByTestId("activity")).toMatchTextContent(/Run updated/);
  await vi.waitFor(() => expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(2));
  await new Promise((r) => setTimeout(r, 300));
  expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(2);
});

test("lists the world's workflows and refreshes the list when one is saved", async () => {
  const weekly = { id: "wf_1", world_id: "world_1", name: "Weekly short", current_version: 1, created_at: video.created_at };
  let listed = [weekly];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json(listed),
  });
  const { screen } = await mountAt("/worlds/world_1");
  await expect.element(screen.getByTestId("workflow-list")).toMatchTextContent(/Weekly short.*v1/);
  await expect
    .element(screen.getByRole("link", { name: "Weekly short" }))
    .toHaveAttribute("href", "/worlds/world_1/workflows/wf_1");
  await expect.element(screen.getByText(/1 saved, each versioned/)).toBeVisible();

  listed = [{ ...weekly, name: "Daily short", current_version: 2 }];
  FakeEventSource.emit("workflow.saved", { seq: 2, type: "workflow.saved", ts: video.created_at });
  await expect.element(screen.getByTestId("workflow-list")).toMatchTextContent(/Daily short.*v2/);
  await expect.element(screen.getByTestId("activity")).toMatchTextContent(/Workflow saved/);
});

test("deleting asks for confirmation, names the loss, then returns to the worlds list", async () => {
  const calls = fakeFetch({
    "GET /api/health": health,
    "GET /api/worlds/world_1": () => Response.json(video),
    "DELETE /api/worlds/world_1": () => new Response(null, { status: 204 }),
    "GET /api/worlds": () => Response.json([]),
  });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  const { screen, router } = await mountAt("/worlds/world_1");
  await screen.getByRole("button", { name: "Delete world" }).click();
  await expect.element(screen.getByRole("heading", { name: "Your worlds" })).toBeVisible();
  expect(confirm.mock.calls[0]?.[0]).toContain("Delete Video?");
  expect(calls).toContain("DELETE /api/worlds/world_1");
  expect(router.state.location.pathname).toBe("/");
});

test("a cancelled confirmation deletes nothing", async () => {
  const calls = fakeFetch({ "GET /api/worlds/world_1": () => Response.json(video) });
  vi.spyOn(window, "confirm").mockReturnValue(false);
  const { screen } = await mountAt("/worlds/world_1");
  await screen.getByRole("button", { name: "Delete world" }).click();
  await expect.element(screen.getByRole("heading", { name: "Video", level: 1 })).toBeVisible();
  expect(calls.filter((c) => c.startsWith("DELETE"))).toEqual([]);
});

test("an unknown world explains itself and links back", async () => {
  fakeFetch({});
  const { screen } = await mountAt("/worlds/world_nope");
  await expect.element(screen.getByRole("heading", { name: "No such world" })).toBeVisible();
  await expect.element(screen.getByRole("link", { name: "Back to your worlds." })).toBeVisible();
});

test("a server error is an alert, not 'No such world'", async () => {
  fakeFetch({ "GET /api/worlds/world_1": () => new Response("boom", { status: 500 }) });
  const { screen } = await mountAt("/worlds/world_1");
  await expect.element(screen.getByRole("alert")).toMatchTextContent(/Could not load this world/);
  await expect.element(screen.getByText("No such world")).not.toBeInTheDocument();
});

test("starting a run of a workflow opens its timeline", async () => {
  const weekly = { id: "wf_1", world_id: "world_1", name: "Weekly short", current_version: 1, created_at: video.created_at };
  const started = {
    id: "run_1", world_id: "world_1", workflow_id: "wf_1", workflow_version: 1, name: "Weekly short",
    status: "queued", created_at: video.created_at, updated_at: video.created_at, stages: [],
  };
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([weekly]),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
    "POST /api/workflows/wf_1/runs": () => Response.json(started, { status: 201 }),
    "GET /api/runs/run_1": () => Response.json(started),
  });
  const { screen, router } = await mountAt("/worlds/world_1");
  await expect.element(screen.getByText(/None yet. Start one/)).toBeVisible();
  await screen.getByRole("button", { name: "Start a run of Weekly short" }).click();
  await expect.element(screen.getByTestId("status-line")).toMatchTextContent(/Queued/);
  expect(calls).toContain("POST /api/workflows/wf_1/runs");
  expect(router.state.location.pathname).toBe("/worlds/world_1/runs/run_1");
});

test("a typed goal starts an ad hoc run and opens its timeline", async () => {
  const bodies: unknown[] = [];
  const started = {
    id: "run_9", world_id: "world_1", workflow_id: null, workflow_version: null, name: "Find five hooks",
    status: "queued", created_at: video.created_at, updated_at: video.created_at, stages: [],
  };
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([]),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
    "POST /api/worlds/world_1/runs": async (req) => {
      bodies.push(await req.json());
      return Response.json(started, { status: 201 });
    },
    "GET /api/runs/run_9": () => Response.json(started),
  });
  const { screen, router } = await mountAt("/worlds/world_1");
  await screen.getByRole("textbox", { name: "Goal" }).fill("Find five hooks");
  await screen.getByRole("button", { name: "Start run" }).click();
  await expect.element(screen.getByTestId("status-line")).toMatchTextContent(/Queued/);
  expect(bodies).toEqual([{ goal: "Find five hooks", done_when: "", baseline: false }]);
  expect(router.state.location.pathname).toBe("/worlds/world_1/runs/run_9");
  await expect.element(screen.getByRole("heading", { level: 1 })).toMatchTextContent(/Find five hooks.*ad hoc/);
});

test("a spent monthly budget is shown as the API's reason", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([]),
    "POST /api/worlds/world_1/runs": () =>
      Response.json({ detail: "the world's monthly budget of $5.00 is spent" }, { status: 409 }),
  });
  const { screen } = await mountAt("/worlds/world_1");
  await screen.getByRole("textbox", { name: "Goal" }).fill("Anything");
  await screen.getByRole("button", { name: "Start run" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("Not started: the world's monthly budget of $5.00 is spent.");
});
