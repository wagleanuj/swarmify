import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, gate, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const timed = (start: string, end: string) => ({
  ...stage(0, "research", "swarm", "done"),
  started_at: start,
  ended_at: end,
});
const researching = run("running", [stage(0, "research", "swarm", "running")], { budget_usd: 5, spent_usd: 1.84 });
const hooli = run("done", [timed("2026-10-26T09:00:00+00:00", "2026-10-26T09:54:00+00:00")], {
  id: "run_0",
  baseline: true,
  spent_usd: 2.6,
});

test("the run page shows Russ against the spend and Hooli beside the swarm, live", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(researching),
    "GET /api/runs/run_1/baseline": () => Response.json(hooli),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("budget")).toHaveTextContent("Russ · $1.84 of $5.00");
  await expect.element(screen.getByTestId("budget-line")).toHaveTextContent("Russ stops every agent at $5.00.");
  await expect.element(screen.getByRole("meter", { name: "Budget spent" })).toHaveAttribute("value", "1.84");
  await expect
    .element(screen.getByTestId("baseline"))
    .toMatchTextContent(/^Hooli ran this alone for \$2\.60 and 54 min\. This run: \$1\.84/);

  FakeEventSource.emit("run.updated", { payload: { ...researching, spent_usd: 4.2 } });
  await expect.element(screen.getByTestId("budget-line")).toHaveTextContent("Past 80 percent. Russ stops every agent at $5.00.");

  const stopped = run("waiting_gate", [stage(0, "research", "swarm", "waiting")], {
    budget_usd: 5,
    spent_usd: 5.2,
    over_budget: true,
  });
  FakeEventSource.emit("run.updated", { payload: stopped });
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Out of budget at stage 1: research. Add money or abort.");
  await expect.element(screen.getByTestId("budget-line")).toHaveTextContent("Russ stopped every agent at $5.00. Add money to go on, or abort.");
  await expect.element(screen.getByRole("link", { name: "Add money or abort" })).toHaveAttribute("href", "/gates");
});

test("with no baseline yet the page says how to get one, and a baseline names itself", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(run("queued", [stage(0, "research", "swarm", "pending")])),
    "GET /api/runs/run_1/baseline": () => Response.json(null),
    "GET /api/runs/run_0": () => Response.json(hooli),
    "GET /api/runs/run_0/baseline": () => Response.json(hooli),
  });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("baseline")).toMatchTextContent(/^No Hooli baseline yet/);
  expect(screen.getByTestId("budget").query()).toBeNull();
  await router.navigate({ to: "/worlds/$worldId/runs/$runId", params: { worldId: "world_1", runId: "run_0" } });
  await expect.element(screen.getByTestId("baseline")).toMatchTextContent(/^This is a Hooli baseline: one agent and no graph/);
});

test("a budget gate adds money as a new total or aborts the run", async () => {
  const bodies: unknown[] = [];
  const posted: string[] = [];
  fakeFetch({
    "GET /api/worlds": () => Response.json([]),
    "GET /api/gates": () => Response.json([gate("budget", { stage_name: "research", budget_usd: 5, spent_usd: 5.2 })]),
    "POST /api/runs/run_1/budget": async (req) => {
      bodies.push(await req.json());
      return Response.json(run("running", []));
    },
    "POST /api/runs/run_1/abort": () => {
      posted.push("abort");
      return Response.json(run("aborted", []));
    },
  });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_1");
  await expect.element(card).toMatchTextContent(/The budget of \$5\.00 is spent.*Russ has spent \$5\.20 of \$5\.00/);
  expect(card.getByRole("textbox").query()).toBeNull(); // no note: money is not a decision
  await card.getByLabelText("Add (US dollars)").fill("2.5");
  await card.getByRole("button", { name: "Add money and go on" }).click();
  await vi.waitFor(() => expect(bodies).toEqual([{ budget_usd: 7.5 }]));

  await card.getByRole("button", { name: "Abort the run" }).click();
  expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Abort Weekly short?"));
  await vi.waitFor(() => expect(posted).toEqual(["abort"]));
});

test("a refused budget is shown as an alert", async () => {
  fakeFetch({
    "GET /api/worlds": () => Response.json([]),
    "GET /api/gates": () => Response.json([gate("budget", { budget_usd: 5, spent_usd: 5.2 })]),
    "POST /api/runs/run_1/budget": () =>
      Response.json({ detail: "the run has spent $5.20; set a budget above that" }, { status: 422 }),
  });
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_1");
  await card.getByLabelText("Add (US dollars)").fill("0.1");
  await card.getByRole("button", { name: "Add money and go on" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("the run has spent $5.20; set a budget above that");
});

test("a workflow starts a Hooli baseline from the world page", async () => {
  const weekly = { id: "wf_1", world_id: "world_1", name: "Weekly short", current_version: 1, created_at: hooli.created_at };
  const bodies: unknown[] = [];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json({ id: "world_1", name: "Video", purpose: "", created_at: hooli.created_at }),
    "GET /api/worlds/world_1/workflows": () => Response.json([weekly]),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
    "POST /api/workflows/wf_1/runs": async (req) => {
      bodies.push(await req.json());
      return Response.json(hooli, { status: 201 });
    },
    "GET /api/runs/run_0": () => Response.json(hooli),
  });
  const { screen, router } = await mountAt("/worlds/world_1");
  await screen.getByRole("button", { name: "Start a Hooli baseline of Weekly short" }).click();
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/worlds/world_1/runs/run_0"));
  expect(bodies).toEqual([{ baseline: true }]);
});

test("the runs list tags a baseline and shows each run's cost", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json({ id: "world_1", name: "Video", purpose: "", created_at: hooli.created_at }),
    "GET /api/worlds/world_1/runs": () => Response.json([hooli]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs");
  await expect.element(screen.getByTestId("run-list")).toMatchTextContent(/Hooli baseline\s*1 of 1 stages\s*\$2\.60/);
});
