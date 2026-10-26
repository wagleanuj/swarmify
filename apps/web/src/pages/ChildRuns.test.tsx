import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, gate, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:00:00+00:00";
const research = { id: "world_2", name: "Research", purpose: "", created_at: ts };
const top = { id: "run_top", world_id: "world_1", name: "Weekly short" };
const mid = { id: "run_mid", world_id: "world_2", name: "Trends" };

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("the runs page links each child run to its parent, in whatever world it lives", async () => {
  const child = run("running", [stage(0, "research", "swarm", "running")], { ...mid, parent: top, parent_run_id: top.id });
  const plain = run("done", [stage(0, "x", "connector", "done")], { id: "run_alone", world_id: "world_2", name: "Alone" });
  fakeFetch({
    "GET /api/worlds/world_2": () => Response.json(research),
    "GET /api/worlds/world_2/runs": () => Response.json([child, plain]),
  });
  const { screen } = await mountAt("/worlds/world_2/runs");
  await expect.element(screen.getByTestId("run-list")).toMatchTextContent(/Trends\s*running\s*child of Weekly short/);
  await expect
    .element(screen.getByRole("link", { name: "Weekly short" }))
    .toHaveAttribute("href", "/worlds/world_1/runs/run_top");
  expect(screen.getByTestId("child-of").elements()).toHaveLength(1);
});

test("a workflow stage links to its child run and the child links back to its parent", async () => {
  const parent = run("running", [stage(0, "trends", "workflow", "running", { child: mid }), stage(1, "pick", "gate", "pending")], {
    ...top,
    spent_usd: 0.25,
  });
  const agents = [
    { id: "a1", run_id: "run_mid", stage_id: "s9", name: "worker-1", status: "done", session_id: null, skills: [], cost_usd: 0.25, created_at: ts, updated_at: ts },
  ];
  fakeFetch({
    "GET /api/runs/run_top": () => Response.json(parent),
    "GET /api/runs/run_top/agents": () => Response.json(agents),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_top");
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Stage 1 of 2, trends, waits on its child run Trends.");
  await expect.element(screen.getByTestId("child-run")).toMatchTextContent("Child run: Trends");
  await expect.element(screen.getByRole("link", { name: "Trends" })).toHaveAttribute("href", "/worlds/world_2/runs/run_mid");
  await expect.element(screen.getByTestId("run-cost")).toMatchTextContent(/\$0\.2500, child runs included/);
});

test("a child run's page names its parent and what it shares with it", async () => {
  const child = run("waiting_gate", [stage(0, "pick", "gate", "waiting")], { ...mid, parent: top, parent_run_id: top.id });
  fakeFetch({ "GET /api/runs/run_mid": () => Response.json(child) });
  const { screen } = await mountAt("/worlds/world_2/runs/run_mid");
  await expect.element(screen.getByTestId("child-of")).toMatchTextContent("child of Weekly short");
  await expect.element(screen.getByText(/spends that run's budget/)).toBeVisible();
  await expect.element(screen.getByRole("link", { name: "Weekly short" })).toHaveAttribute("href", "/worlds/world_1/runs/run_top");
});

test("the gates inbox labels a child run's gate with its parent", async () => {
  fakeFetch({
    "GET /api/worlds": () => Response.json([research]),
    "GET /api/gates": () => Response.json([gate("pick", { run_id: "run_mid", world_id: "world_2", run_name: "Trends", parent_run_id: top.id, parent: top })]),
  });
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_mid");
  await expect.element(card).toMatchTextContent(/Trends\s*Research · stage 2 · pick · waiting since .*child of Weekly short/);
  await expect.element(card.getByRole("link", { name: "Weekly short" })).toHaveAttribute("href", "/worlds/world_1/runs/run_top");
});
