import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, run } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:00:00+00:00";
const video = { id: "world_1", name: "Video", purpose: "", created_at: ts };
const stage = (index: number, status: string) => ({
  id: `stage_${index}`,
  index,
  name: `s${index}`,
  type: "connector",
  status,
  started_at: null,
  ended_at: null,
  error: null,
});

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("lists runs with status and progress, updating and adding them live", async () => {
  const first = run("running", [stage(0, "done"), stage(1, "running"), stage(2, "pending")]);
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/runs": () => Response.json([first]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs");
  await expect.element(screen.getByTestId("run-list")).toMatchTextContent(/Render then pick\s*running\s*1 of 3 stages/);
  await expect
    .element(screen.getByRole("link", { name: "Render then pick" }))
    .toHaveAttribute("href", "/worlds/world_1/runs/run_1");

  FakeEventSource.emit("run.updated", {
    payload: run("waiting_gate", [stage(0, "done"), stage(1, "done"), stage(2, "waiting")]),
  });
  await expect.element(screen.getByTestId("run-list")).toMatchTextContent(/needs you\s*2 of 3 stages/);

  FakeEventSource.emit("run.updated", {
    payload: run("queued", [stage(0, "pending")], { id: "run_2", name: "Second" }),
  });
  await expect.element(screen.getByTestId("run-list")).toMatchTextContent(/^Second\s*queued/);
});

test("with no runs it says what the page is for and where to start one", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs");
  await expect.element(screen.getByTestId("no-runs")).toMatchTextContent(/follows one workflow/);
  await expect
    .element(screen.getByRole("link", { name: "Start one from a workflow" }))
    .toHaveAttribute("href", "/worlds/world_1");
});

test("a load failure is an alert", async () => {
  fakeFetch({ "GET /api/worlds/world_1/runs": () => new Response("boom", { status: 500 }) });
  const { screen } = await mountAt("/worlds/world_1/runs");
  await expect.element(screen.getByRole("alert")).toMatchTextContent(/Could not load runs/);
});
