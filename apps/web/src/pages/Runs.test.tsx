import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, run, stage as shared } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:00:00+00:00";
const video = { id: "world_1", name: "Video", purpose: "", created_at: ts };
const stage = (index: number, status: string) => shared(index, `s${index}`, "connector", status);

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

test("filters by workflow, ad hoc and status, and shows how long each run took", async () => {
  const at = (minute: number) => `2026-10-26T10:0${minute}:00+00:00`;
  const timed = shared(0, "s0", "swarm", "done", { started_at: at(0), ended_at: at(2) });
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () =>
      Response.json([
        { id: "wf_1", world_id: "world_1", name: "Render then pick", current_version: 2, created_at: ts },
        { id: "wf_2", world_id: "world_1", name: "Weekly", current_version: 1, created_at: ts },
      ]),
    "GET /api/worlds/world_1/runs": () =>
      Response.json([
        run("running", [stage(0, "running")], { id: "run_3", name: "Weekly run", workflow_id: "wf_2" }),
        run("failed", [stage(0, "failed")], { id: "run_2", name: "Hooks", workflow_id: null, workflow_version: null }),
        run("done", [timed]),
      ]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs");
  const list = screen.getByTestId("run-list");
  await expect.element(list).toMatchTextContent(/Render then pick\s*done.*took 2 min/);
  const names = () => [...list.element().querySelectorAll("li > a")].map((a) => a.textContent);

  await screen.getByRole("combobox", { name: "Workflow" }).selectOptions("Weekly");
  await expect.poll(names).toEqual(["Weekly run"]);
  await screen.getByRole("combobox", { name: "Workflow" }).selectOptions("Ad hoc");
  await expect.poll(names).toEqual(["Hooks"]);
  await screen.getByRole("combobox", { name: "Workflow" }).selectOptions("All workflows");
  await screen.getByRole("combobox", { name: "Status" }).selectOptions("done");
  await expect.poll(names).toEqual(["Render then pick"]);
  await screen.getByRole("combobox", { name: "Workflow" }).selectOptions("Weekly");
  await expect.element(screen.getByTestId("no-match")).toMatchTextContent(/No runs match/);
});
