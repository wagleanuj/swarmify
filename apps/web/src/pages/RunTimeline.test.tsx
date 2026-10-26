import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const queued = run("queued", [
  stage(0, "render", "connector", "pending"),
  stage(1, "pick", "gate", "pending"),
]);

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("renders the stages in order and follows the run live from the world stream", async () => {
  fakeFetch({ "GET /api/runs/run_1": () => Response.json(queued) });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Render then pick v2");
  await expect.element(screen.getByTestId("status-line")).toMatchTextContent(/Queued/);
  await expect.element(screen.getByTestId("timeline")).toMatchTextContent(/1 · pending · connector\s*render/);
  expect(FakeEventSource.open.map((s) => s.url)).toEqual(["/api/worlds/world_1/events"]);

  const running = run("running", [stage(0, "render", "connector", "running"), stage(1, "pick", "gate", "pending")]);
  FakeEventSource.emit("run.updated", { payload: running });
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Running stage 1 of 2: render.");

  const other = run("failed", [stage(0, "x", "connector", "failed")], { id: "run_2" });
  FakeEventSource.emit("run.updated", { payload: other });
  const waiting = run("waiting_gate", [stage(0, "render", "connector", "done"), stage(1, "pick", "gate", "waiting")]);
  FakeEventSource.emit("run.updated", { payload: waiting });
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Waiting for you at stage 2: pick.");
  await expect.element(screen.getByTestId("timeline")).toMatchTextContent(/1 · done · connector\s*render\s*Show the call\s*2 · you · gate\s*pick/);
});

test("a failed stage shows its error", async () => {
  const failed = run("failed", [
    { ...stage(0, "render", "connector", "failed"), error: "RuntimeError: no" },
    stage(1, "pick", "gate", "pending"),
  ]);
  fakeFetch({ "GET /api/runs/run_1": () => Response.json(failed) });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Failed at stage 1, render: RuntimeError: no.");
  await expect.element(screen.getByTestId("timeline")).toMatchTextContent(/RuntimeError: no/);
});

test("an unknown run explains itself and links back to runs", async () => {
  fakeFetch({});
  const { screen } = await mountAt("/worlds/world_1/runs/run_nope");
  await expect.element(screen.getByRole("heading", { name: "No such run" })).toBeVisible();
  await expect
    .element(screen.getByRole("link", { name: "Back to runs." }))
    .toHaveAttribute("href", "/worlds/world_1/runs");
});

const call = {
  idempotency_key: "stage_0",
  world_id: "world_1",
  run_id: "run_1",
  connector: "fal_video",
  tool: "fal_video",
  request: { idempotency_key: "stage_0", output: "clip.mp4" },
  status: "polling",
  started_at: "2026-10-26T10:00:00+00:00",
  updated_at: "2026-10-26T10:00:00+00:00",
  response: { job_id: "j1", poll: "fal_status" },
  error: null,
  job_id: "j1",
  poll_tool: "fal_status",
  progress: 0.25,
  message: "frames",
  attempts: 1,
  polls: 1,
};

test("opening a connector stage shows its raw call, and progress follows the stream", async () => {
  const rendering = run("running", [stage(0, "clip", "connector", "running"), stage(1, "pick", "gate", "pending")]);
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(rendering),
    "GET /api/stages/stage_0/call": () => Response.json(call),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await screen.getByRole("button", { name: "Show the call" }).click();
  const panel = screen.getByTestId("call");
  await expect.element(panel).toMatchTextContent(/fal_video · fal_video\s*polling/);
  await expect.element(panel).toMatchTextContent(/In: nothing · Out: clip\.mp4 · attempt 1 · polled 1 times · key stage_0/);
  await expect.element(screen.getByTestId("call-request")).toMatchTextContent(/"idempotency_key": "stage_0"/);
  await expect.element(screen.getByTestId("call-response")).toMatchTextContent(/"job_id": "j1"/);
  await expect.element(screen.getByTestId("timeline")).toMatchTextContent(/25% · frames/);

  FakeEventSource.emit("connector_call.updated", { payload: { ...call, run_id: "run_2", progress: 0.9 } });
  FakeEventSource.emit("connector_call.updated", {
    payload: { ...call, status: "done", progress: 1, response: { url: "https://cdn/clip.mp4" } },
  });
  await expect.element(screen.getByTestId("call-response")).toMatchTextContent(/cdn\/clip\.mp4/);
  await expect.element(screen.getByTestId("timeline")).not.toMatchTextContent(/%/);
  await screen.getByRole("button", { name: "Hide the call" }).click();
  expect(screen.getByTestId("call").elements()).toHaveLength(0);
});

test("a connector stage that has not started yet says so", async () => {
  fakeFetch({ "GET /api/runs/run_1": () => Response.json(queued) });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await screen.getByRole("button", { name: "Show the call" }).click();
  await expect.element(screen.getByTestId("call")).toMatchTextContent(/No call yet/);
});
