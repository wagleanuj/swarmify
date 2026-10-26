import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Agent, LogEntry } from "../api/events";
import { FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:00:01+00:00";
const researching = run("running", [stage(0, "research", "swarm", "running")]);
const agent = (over: Partial<Agent> = {}): Agent => ({
  id: "agent_1",
  world_id: "world_1",
  run_id: "run_1",
  stage_id: "stage_0",
  name: "worker-1",
  role: "worker",
  status: "working",
  session_id: "session-1",
  cost_usd: 0.0123,
  created_at: ts,
  updated_at: ts,
  skills: ["world_1:hooks"],
  ...over,
});
const entry = (seq: number, over: Partial<LogEntry> = {}): LogEntry => ({
  seq,
  agent: "worker-1",
  stage_id: "stage_0",
  kind: "tool",
  ts,
  tool: "mcp__swarmify__query_graph",
  input: {},
  detail: "",
  ...over,
});

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("a swarm stage shows its agents, their estimated cost and a live log", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(researching),
    "GET /api/runs/run_1/agents": () => Response.json([agent()]),
    "GET /api/runs/run_1/stages/stage_0/log": () => Response.json([entry(1)]),
    "GET /api/runs/run_1/stages/stage_0/tasks": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("agent")).toMatchTextContent(/Gilfoyle\s*working.*estimated \$0\.0123.*skills: world_1:hooks/);
  await expect.element(screen.getByTestId("run-cost")).toMatchTextContent(/\$0\.0123\. An estimate/);
  await expect.element(screen.getByTestId("log")).toMatchTextContent(/10:00:01 Gilfoyle used query_graph/);

  FakeEventSource.emit("agent.logged", {
    payload: entry(2, { kind: "denied", tool: "Bash", detail: "Bash is not allowed in this stage." }),
  });
  FakeEventSource.emit("agent.logged", { payload: entry(1) });
  FakeEventSource.emit("agent.logged", { payload: entry(3, { stage_id: "stage_9", tool: "elsewhere" }) });
  FakeEventSource.emit("agent.updated", { payload: agent({ status: "done", cost_usd: 0.05 }) });
  await expect.element(screen.getByTestId("log")).toMatchTextContent(/used query_graph\s*10:00:01 Gilfoyle was refused Bash: Bash is not allowed in this stage\.$/);
  await expect.element(screen.getByTestId("agent")).toMatchTextContent(/done.*\$0\.0500/);
  await expect.element(screen.getByTestId("run-cost")).toMatchTextContent(/\$0\.0500/);
});

test("before any agent starts the panel says what will appear", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(researching),
    "GET /api/runs/run_1/agents": () => Response.json([]),
    "GET /api/runs/run_1/stages/stage_0/log": () => Response.json([]),
    "GET /api/runs/run_1/stages/stage_0/tasks": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("agents")).toMatchTextContent(/No agent has started yet.*Every tool call appears here/);
  expect(screen.getByTestId("run-cost").query()).toBeNull();
});
