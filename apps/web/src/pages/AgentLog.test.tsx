import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Agent, LogEntry } from "../api/events";
import { FakeEventSource, fakeFetch, run, stage, task } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:00:01+00:00";
const researching = run("running", [stage(0, "research", "swarm", "running")], { spent_usd: 0.0123 });
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
  model: "claude-haiku-4-5",
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

test("a swarm stage shows its agents as cards saying what each does now, and a live log in words", async () => {
  const claim = entry(2, { tool: "mcp__swarmify__claim_task", input: { task_id: "task_1" } });
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(researching),
    "GET /api/runs/run_1/agents": () => Response.json([agent({ skills: ["world_1:hooks", "deep-research"] })]),
    "GET /api/runs/run_1/stages/stage_0/log": () => Response.json([entry(1), claim]),
    "GET /api/runs/run_1/stages/stage_0/tasks": () => Response.json([task("task_1", "claimed", { title: "Fact-check the candidates" })]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  const card = screen.getByTestId("agent");
  await expect.element(card).toMatchTextContent(/Worker 1\s*working\s*worker · \$0\.0123 · claude-haiku-4-5/);
  await expect.element(card.getByTestId("doing")).toHaveTextContent("Claimed: Fact-check the candidates");
  await expect.element(card.getByText("2 skills")).toBeVisible();
  await expect.element(card.getByText("world_1:hooks, deep-research")).not.toBeVisible();
  await expect.element(screen.getByTestId("log")).toMatchTextContent(/10:00:01 Worker 1: Searching the world's memory/);
  expect(screen.getByTestId("run-cost").query()).toBeNull(); // one spend line, under the result

  FakeEventSource.emit("agent.logged", {
    payload: entry(3, { tool: "WebSearch", input: { query: "kookaburra call" } }),
  });
  FakeEventSource.emit("agent.logged", { payload: entry(1) });
  FakeEventSource.emit("agent.logged", { payload: entry(4, { stage_id: "stage_9", tool: "elsewhere" }) });
  await expect.element(card.getByTestId("doing")).toHaveTextContent('Searching the web for "kookaburra call"');
  FakeEventSource.emit("agent.logged", {
    payload: entry(5, { kind: "denied", tool: "Bash", detail: "Bash is not allowed in this stage." }),
  });
  FakeEventSource.emit("agent.updated", { payload: agent({ status: "done", cost_usd: 0.05 }) });
  await expect.element(screen.getByTestId("log")).toMatchTextContent(/kookaburra call"\s*10:00:01 Worker 1: Refused Bash: Bash is not allowed in this stage\.$/);
  await expect.element(card).toMatchTextContent(/done\s*worker · \$0\.0500.*Finished/);
});

test("a JSON answer in the log collapses to one line that opens", async () => {
  const planned = entry(1, {
    agent: "planner",
    kind: "result",
    tool: null,
    detail: 'Session ended at an estimated $0.2105. {"tasks":[{"title":"a"},{"title":"b"}]}',
  });
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(researching),
    "GET /api/runs/run_1/stages/stage_0/log": () => Response.json([planned]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  const log = screen.getByTestId("log");
  await expect.element(log).toMatchTextContent(/Planner: Session ended at an estimated \$0\.2105\.\s*Planned 2 tasks\{/);
  await expect.element(log.getByText('"title": "a"', { exact: false })).not.toBeVisible();
  await log.getByText("Planned 2 tasks").click();
  await expect.element(log.getByText('"title": "a"', { exact: false })).toBeVisible();
});

test("before any agent starts the panel says what will appear", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json({ ...researching, spent_usd: 0 }),
    "GET /api/runs/run_1/agents": () => Response.json([]),
    "GET /api/runs/run_1/stages/stage_0/log": () => Response.json([]),
    "GET /api/runs/run_1/stages/stage_0/tasks": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("agents")).toMatchTextContent(/No agent has started yet/);
  await expect.element(screen.getByRole("region", { name: "Live log" })).toMatchTextContent(/Every step an agent takes appears here/);
});
