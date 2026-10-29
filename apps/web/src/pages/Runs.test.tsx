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
  await expect.element(screen.getByTestId("run-list")).toMatchTextContent(/Render then pick.*running/);
  await expect
    .element(screen.getByRole("link", { name: "Render then pick" }))
    .toHaveAttribute("href", "/worlds/world_1/runs/run_1");

  FakeEventSource.emit("run.updated", {
    payload: run("waiting_gate", [stage(0, "done"), stage(1, "done"), stage(2, "waiting")]),
  });
  await expect.element(screen.getByTestId("run-list")).toMatchTextContent(/Render then pick.*needs you/);

  FakeEventSource.emit("run.updated", {
    payload: run("queued", [stage(0, "pending")], { id: "run_2", name: "Second" }),
  });
  await expect.element(screen.getByTestId("run-list")).toMatchTextContent(/Second.*queued.*Render then pick/);
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

const at = (minute: number) => `2026-10-26T10:${String(minute).padStart(2, "0")}:00+00:00`;
const timed = (index: number, name: string, type: string, from: number, to: number) =>
  shared(index, name, type, "done", { started_at: at(from), ended_at: at(to) });

test("status tabs live in the address, count the rows and say when a tab is empty", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/runs": () =>
      Response.json([
        run("waiting_gate", [stage(0, "waiting")], { id: "run_4", name: "Asks" }),
        run("running", [stage(0, "running")], { id: "run_3", name: "Goes" }),
        run("aborted", [stage(0, "pending")], { id: "run_2", name: "Stopped" }),
        run("failed", [stage(0, "failed")], { id: "run_1", name: "Broke" }),
      ]),
  });
  const { router, screen } = await mountAt("/worlds/world_1/runs?status=failed");
  const names = () => [...document.querySelectorAll("[data-testid=run-list] tbody td:first-child a")].map((a) => a.textContent);
  await expect.poll(names).toEqual(["Stopped", "Broke"]);
  const tabs = screen.getByRole("navigation", { name: "Status" });
  await expect.element(tabs).toMatchTextContent(/All 4\s*Waiting for you 1\s*Running 1\s*Done 0\s*Failed or aborted 2/);
  await expect.element(screen.getByRole("link", { name: "Failed or aborted 2" })).toHaveAttribute("aria-current", "page");
  await expect.element(screen.getByTestId("run-list")).toMatchTextContent(/2 runs/);

  await screen.getByRole("link", { name: "Waiting for you 1" }).click();
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ status: "waiting" }));
  await expect.poll(names).toEqual(["Asks"]);

  await screen.getByRole("link", { name: "Done 0" }).click();
  await expect.element(screen.getByTestId("no-match")).toMatchTextContent("No runs done in this world.");
  await screen.getByRole("link", { name: "All 4" }).click();
  await vi.waitFor(() => expect(router.state.location.search).toEqual({}));
  await expect.poll(names).toHaveLength(4);
});

test("each row carries its steps strip, started, time and spend, with a total row", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/runs": () =>
      Response.json([
        run("done", [timed(0, "render", "swarm", 0, 2), stage(1, "skipped")], { spent_usd: 1.5 }),
        run("queued", [stage(0, "pending")], { id: "run_2", name: "Later", spent_usd: 0.25 }),
      ]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs");
  const list = screen.getByTestId("run-list");
  await expect.element(list).toMatchTextContent(/Render then pick\s*v2\s*run_1\s*done/);
  await expect.element(list).toMatchTextContent(/2m 0s\s*\$1\.50/);
  await expect.element(list).toMatchTextContent(/2 runs\s*\$1\.75/);
  const first = list.getByRole("row").nth(1);
  await expect.element(first.getByRole("img", { name: "render · 2m 0s" })).toBeInTheDocument();
  await expect.element(first.getByRole("img", { name: "s1 · skipped" })).toBeInTheDocument();
});

test("where the time went and spend by version come from the runs", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () =>
      Response.json([{ id: "wf_1", world_id: "world_1", name: "Render then pick", current_version: 2, created_at: ts }]),
    "GET /api/worlds/world_1/runs": () =>
      Response.json([
        run("done", [timed(0, "render", "swarm", 0, 2), timed(1, "approve", "gate", 2, 12)], { id: "run_3", spent_usd: 1 }),
        run("done", [timed(0, "render", "swarm", 0, 4)], { id: "run_2", spent_usd: 3 }),
        run("done", [timed(0, "hook", "swarm", 0, 1)], { id: "run_1", workflow_version: 1, spent_usd: 0.5 }),
        run("done", [], { id: "run_0", name: "Typed", workflow_id: null, workflow_version: null, spent_usd: 0.1 }),
      ]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs");
  await expect
    .element(screen.getByTestId("time-went"))
    .toMatchTextContent(/approve\s*waiting for you\s*10m 0s\s*render\s*agents\s*6m 0s\s*hook\s*agents\s*1m 0s/);
  await expect
    .element(screen.getByTestId("spend-by-version"))
    .toMatchTextContent(/Render then pick v2\s*2\s*\$4\.00\s*\$2\.00\s*Render then pick v1\s*1\s*\$0\.50\s*\$0\.50\s*Ad hoc\s*1\s*\$0\.10\s*\$0\.10\s*All\s*4\s*\$4\.60/);
  await expect.element(screen.getByText(/Most of the time went to approve: 10m 0s across these runs/)).toBeInTheDocument();
});

test("filters by workflow and ad hoc, and shows how long each run took", async () => {
  const done = timed(0, "s0", "swarm", 0, 2);
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
        run("done", [done]),
      ]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs");
  const list = screen.getByTestId("run-list");
  await expect.element(list).toMatchTextContent(/Render then pick\s*v2.*done.*2m 0s/);
  const names = () => [...list.element().querySelectorAll("tbody td:first-child a")].map((a) => a.textContent);

  await screen.getByRole("combobox", { name: "Workflow" }).selectOptions("Weekly");
  await expect.poll(names).toEqual(["Weekly run"]);
  await screen.getByRole("combobox", { name: "Workflow" }).selectOptions("Ad hoc");
  await expect.poll(names).toEqual(["Hooks"]);
  await screen.getByRole("link", { name: /^Done/ }).click();
  await expect.element(screen.getByTestId("no-match")).toMatchTextContent(/No runs done/);
});
