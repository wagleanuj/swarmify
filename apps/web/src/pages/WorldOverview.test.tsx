import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";

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

  expect(FakeEventSource.open.map((s) => s.url)).toEqual(["/api/worlds/world_1/events"]);
  FakeEventSource.emit("world.created", { seq: 1, type: "world.created", ts: video.created_at });
  await expect.element(screen.getByTestId("activity")).toMatchTextContent(/world\.created/);
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
  await expect.element(screen.getByTestId("activity")).toMatchTextContent(/workflow\.saved/);
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
