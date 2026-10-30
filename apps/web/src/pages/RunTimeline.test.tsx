import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import type { ArtifactFile, LogEntry } from "../api/events";
import type { Call } from "../api/runs";
import { FakeEventSource, fakeFetch, gate, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const at = (minute: number, second = 0) => `2026-10-26T10:${String(minute).padStart(2, "0")}:${String(second).padStart(2, "0")}+00:00`;
const timed = (start: number, end: number | null) => ({ started_at: at(start), ended_at: end === null ? null : at(end, 14) });
const world = { id: "world_1", name: "Kids videos", purpose: "", created_at: at(0) };

/** An artifact as `GET /api/runs/{id}/artifacts` lists it. */
function made(stageId: string, name: string, over: Partial<ArtifactFile> = {}): ArtifactFile {
  return {
    name,
    path: `runs/run_1/${stageId}/artifacts/${name}`,
    type: "file",
    author: "worker-1",
    title: name,
    fields: {},
    body: "",
    run_id: "run_1",
    stage: stageId,
    created: at(5),
    pinned: false,
    ...over,
  };
}

const call = (stageId: string, over: Partial<Call> = {}): Call => ({
  idempotency_key: stageId,
  world_id: "world_1",
  run_id: "run_1",
  connector: "remotion_render",
  tool: "remotion_render",
  request: { project: "/data/worlds/world_1/runs/run_1/stage_2/build", composition: "Main" },
  status: "polling",
  started_at: at(3),
  updated_at: at(3),
  response: null,
  error: null,
  job_id: "j1",
  poll_tool: "remotion_render_status",
  progress: 0.4,
  message: "rendered 412/870 frames",
  attempts: 1,
  polls: 3,
  visit: 0,
  ...over,
});

const log = (seq: number, over: Partial<LogEntry>): LogEntry => ({
  seq,
  agent: "builder",
  stage_id: "stage_2",
  kind: "tool",
  ts: at(2),
  tool: "Bash",
  input: {},
  detail: "",
  ...over,
});

const research = (status: string, t = timed(0, 2)) => stage(0, "research", "swarm", status, t);
const voice = (status: string, t = timed(2, 3)) => stage(1, "voice", "connector", status, { ...t, calls: "speech" });
const video = (status: string, t = timed(3, 7)) => stage(2, "video", "connector", status, { ...t, calls: "remotion_build" });
const review = (status: string, t = timed(7, 9)) =>
  stage(3, "review", "gate", status, { ...t, ask: "Watch it. Approve or send back." });
const publish = (status: string, t = timed(9, 10)) => stage(4, "publish", "connector", status, { ...t, calls: "youtube" });

const timings = made("stage_1", "timings.json", {
  body: JSON.stringify({
    engine: "http://localhost:8881",
    takes: [
      { scene: "hook", file: "narration/hook.wav", duration: 3.56, lines: [{ text: "Shh.", joined_next: true }, { text: "Listen." }] },
      { scene: "payoff", file: "narration/payoff.wav", duration: 4.1, lines: [{ text: "A chick!" }] },
    ],
  }),
});
const mp4 = made("stage_2", "video.mp4", { path: "runs/run_1/stage_2/artifacts/video.mp4.md", type: "video", title: "video.mp4" });
const posted = made("stage_4", "publish.md", {
  type: "publish",
  title: "Kids short",
  fields: { connector: "youtube", title: "Kids short", visibility: "private", links: { youtube: "https://youtu.be/abc" }, views: 1234, views_at: "2026-10-27T00:00:00" },
});

/** The run menu, opened. */
async function runMenu(screen: Awaited<ReturnType<typeof mountAt>>["screen"]) {
  await screen.getByRole("button", { name: "Run actions" }).click();
  return screen.getByRole("menu", { name: "Run actions" });
}

beforeEach(async () => {
  FakeEventSource.install();
  await page.viewport(1280, 800);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("running: the status line says which stage works, the step and the time, and a new run is never an empty rail", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(at(4)));
  const queued = run("queued", [research("pending", timed(0, null)), voice("pending"), video("pending")].map((s) => ({ ...s, started_at: null, ended_at: null })));
  const calls = fakeFetch({
    "GET /api/runs/run_1": () => Response.json(queued),
    "GET /api/worlds/world_1": () => Response.json(world),
  });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("status-line")).toMatchTextContent(/^Queued\./);
  await expect.element(screen.getByRole("button", { name: "Step 1, research, starting" })).toHaveAttribute("aria-current", "step");
  // Arriving with no step pins the picked one in the address.
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ stage: "stage_0" }));
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Render then pick v2");
  // Beside the shell's one all-worlds stream.
  expect(FakeEventSource.open.map((s) => s.url).sort()).toEqual(["/api/events?since=now", "/api/worlds/world_1/events"]);

  const running = run("running", [research("done"), voice("done"), video("running", timed(3, null))], { updated_at: at(4) });
  FakeEventSource.emit("run.updated", { payload: running });
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Video is working · step 3 of 3 · 4 min so far");
  const rail = screen.getByTestId("timeline");
  await expect.element(rail).toMatchTextContent(/research\s*swarm\s*2m 14s\s*voice\s*speech\s*1m 14s\s*video\s*build\s*running/);
  // The run moved on; the step you are on stays, its log is not fetched again, and Go there follows.
  await expect.element(screen.getByTestId("follow")).toHaveTextContent("Now at video · Go there");
  await expect.element(screen.getByRole("button", { name: /^Step 1, research/ })).toHaveAttribute("aria-pressed", "true");
  expect(router.state.location.search).toEqual({ stage: "stage_0" });
  expect(calls.filter((c) => c.endsWith("/log"))).toEqual(["GET /api/runs/run_1/stages/stage_0/log"]);
  await screen.getByRole("button", { name: "Go there" }).click();
  await expect.element(screen.getByRole("button", { name: "Step 3, video, running" })).toHaveAttribute("aria-pressed", "true");
  expect(router.state.location.search).toEqual({ stage: "stage_2" });
  await expect.element(screen.getByTestId("follow")).not.toBeInTheDocument();
  // Nothing made yet, so no step leads with a result.
  expect(screen.getByTestId("result").query()).toBeNull();

  // An older snapshot replayed from the stream never moves the page back.
  FakeEventSource.emit("run.updated", { payload: { ...queued, updated_at: at(1) } });
  await expect.element(screen.getByTestId("status-line")).toMatchTextContent(/^Video is working/);
  vi.useRealTimers();
});

test("at a gate: the status line asks the gate's question, and the gate's panel leads with the video and the answer", async () => {
  const waiting = run("waiting_gate", [research("done"), voice("done"), video("done"), review("waiting", timed(7, null))]);
  const decided: unknown[] = [];
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "GET /api/runs/run_1/artifacts": () => Response.json([timings, mp4]),
    "GET /api/gates": () => Response.json([gate("approve", { stage_index: 3, stage_id: "stage_3", earlier_stages: ["video"] })]),
    "POST /api/runs/run_1/decision": async (req) => {
      decided.push(await req.json());
      return Response.json(waiting);
    },
  });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1");
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ stage: "stage_3" }));
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Waiting for you: Watch it. Approve or send back.");
  // The thing under review and the answer lead the step's panel; no run link, the page names it.
  const panel = screen.getByTestId("stage-panel");
  await expect.element(panel).toMatchTextContent(/^review\s*gate · step 4 of 4 · waiting\s*Watch it\. Approve or send back\./);
  await expect.element(panel.getByTestId("player")).toHaveAttribute("src", "/api/worlds/world_1/files/runs/run_1/stage_2/artifacts/video.mp4");
  await expect.element(panel.getByRole("button", { name: "Approve" })).toBeVisible();
  expect(panel.getByRole("link", { name: "Weekly short" }).query()).toBeNull();
  expect(screen.getByRole("button", { name: /^Answer/ }).query()).toBeNull();

  // On another step the title row offers the way back to the gate.
  await screen.getByRole("button", { name: /^Step 1, research/ }).click();
  await screen.getByRole("button", { name: "Answer review" }).click();
  expect(router.state.location.search).toEqual({ stage: "stage_3" });
  await panel.getByRole("button", { name: "Approve" }).click();
  await vi.waitFor(() => expect(decided).toMatchObject([{ note: "", artifact: mp4.path }]));

  // Approving moves the run on, not the page: the gate step stays selected.
  FakeEventSource.emit("run.updated", {
    payload: run("running", [research("done"), voice("done"), video("done"), review("done"), publish("running", timed(9, null))], { updated_at: at(30) }),
  });
  await expect.element(screen.getByTestId("follow")).toHaveTextContent("Now at publish · Go there");
  await expect.element(screen.getByRole("button", { name: /^Step 4, review/ })).toHaveAttribute("aria-pressed", "true");
  expect(router.state.location.search).toEqual({ stage: "stage_3" });
});

test("answered on the run page: you stay on the step, see what you decided, and choose where to go", async () => {
  const waiting = run("waiting_gate", [research("done"), voice("done"), video("done"), review("waiting", timed(7, null))]);
  const other = gate("pick", { run_id: "run_2", run_name: "Bedtime story", stage_id: "stage_9", stage_name: "pick idea" });
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "GET /api/runs/run_1/artifacts": () => Response.json([timings, mp4]),
    "GET /api/gates": () => Response.json([gate("approve", { stage_index: 3, stage_id: "stage_3", earlier_stages: ["video"] }), other]),
    "POST /api/runs/run_1/decision": () => Response.json(waiting),
  });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_3");
  const panel = screen.getByTestId("stage-panel");
  const scrolled = window.scrollY;
  await panel.getByLabelText(/^Note/).fill("Good pace.");
  await panel.getByRole("button", { name: "Approve" }).click();
  const answered = panel.getByTestId("answered");
  await expect.element(answered).toMatchTextContent(/^You approved it at \d/);
  // Another gate is open elsewhere: the next decision opens it in Needs you.
  await expect
    .element(answered.getByRole("link", { name: "Next decision: pick idea on Bedtime story" }))
    .toHaveAttribute("href", "/gates?gate=stage_9");
  expect(panel.getByRole("button", { name: "Approve" }).query()).toBeNull();

  // The run moves on; the page does not.
  FakeEventSource.emit("run.updated", {
    payload: run("running", [research("done"), voice("done"), video("done"), review("done"), publish("running", timed(9, null))], {
      updated_at: at(30),
      decisions: [{ id: "d1", stage_index: 3, stage_name: "review", kind: "approve", choice: null, note: "Good pace.", send_back_to: null, path: "/x", decided_at: at(9) }],
    }),
  });
  await expect.element(answered).toMatchTextContent(/The run moved on to publish\./);
  await expect.element(panel.getByTestId("decision")).toHaveTextContent("You approved. Your note: Good pace.");
  expect(router.state.location.search).toEqual({ stage: "stage_3" });
  expect(window.scrollY).toBe(scrolled);
  await answered.getByRole("button", { name: "Watch publish" }).click();
  await expect.element(screen.getByRole("button", { name: /^Step 5, publish/ })).toHaveAttribute("aria-pressed", "true");
  expect(router.state.location.search).toEqual({ stage: "stage_4" });
});

test("answered by a send back: it names the target, does not select it, and offers no next decision when none is open", async () => {
  const waiting = run("waiting_gate", [research("done"), voice("done"), video("done"), review("waiting", timed(7, null))]);
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "GET /api/runs/run_1/artifacts": () => Response.json([timings, mp4]),
    "GET /api/gates": () => Response.json([gate("approve", { stage_index: 3, stage_id: "stage_3", earlier_stages: ["video"] })]),
    "POST /api/runs/run_1/decision": () => Response.json(waiting),
  });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_3");
  const panel = screen.getByTestId("stage-panel");
  await panel.getByRole("button", { name: "Send back to..." }).click();
  await panel.getByRole("menuitem", { name: /^video/ }).click();
  const answered = panel.getByTestId("answered");
  await expect.element(answered).toMatchTextContent(/^You sent it back to video at \d/);
  FakeEventSource.emit("run.updated", {
    payload: run("running", [research("done"), voice("done"), video("running", timed(3, null)), review("pending")], { updated_at: at(30) }),
  });
  await expect.element(answered).toMatchTextContent(/video is running again\./);
  await expect.element(answered.getByRole("button", { name: "Watch video" })).toBeVisible();
  expect(answered.getByRole("link", { name: /^Next decision/ }).query()).toBeNull();
  expect(router.state.location.search).toEqual({ stage: "stage_3" });
});

test("answered by a pick: the step says what you picked", async () => {
  const pickStage = stage(1, "choose", "gate", "waiting", { ...timed(2, null), ask: "Pick one idea." });
  const waiting = run("waiting_gate", [research("done"), pickStage]);
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "GET /api/runs/run_1/artifacts": () => Response.json([]),
    "GET /api/gates": () => Response.json([gate("pick", { stage_name: "choose" })]),
    "POST /api/runs/run_1/decision": () => Response.json(waiting),
  });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_1");
  const panel = screen.getByTestId("stage-panel");
  await panel.getByLabelText("Your pick").fill("idea 2");
  await panel.getByRole("button", { name: "Continue with this pick" }).click();
  await expect.element(panel.getByTestId("answered")).toMatchTextContent(/^You picked "idea 2" at \d/);
  expect(router.state.location.search).toEqual({ stage: "stage_1" });
});

test("done with a video: the run's time, where it was posted, and the video in place", async () => {
  const done = run("done", [research("done"), voice("done"), video("done"), review("done"), publish("done")], {
    budget_usd: 40,
    spent_usd: 5.0565,
    decisions: [
      { id: "d1", stage_index: 3, stage_name: "review", kind: "approve", choice: "video.mp4", note: "Good pace.", send_back_to: null, path: "/x", decided_at: at(9) },
    ],
  });
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(done),
    "GET /api/runs/run_1/artifacts": () => Response.json([timings, mp4, posted]),
    "GET /api/stages/stage_2/call": () => Response.json(call("stage_2", { status: "done", progress: 1, response: { output: "video.mp4" } })),
    "GET /api/stages/stage_4/call": () => Response.json(call("stage_4", { connector: "youtube", tool: "youtube", status: "done" })),
  });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Done in 10 min. Posted private to YouTube.");
  // A done run opens on the step that made its result, not the last one to finish.
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ stage: "stage_2" }));
  await expect.element(screen.getByTestId("follow")).not.toBeInTheDocument();
  await expect.element(screen.getByRole("link", { name: "Open the post" }).first()).toHaveAttribute("href", "https://youtu.be/abc");
  const panel = screen.getByTestId("stage-panel");
  await expect.element(panel).toMatchTextContent(/^video\s*build · step 3 of 5 · 4m 14s/);
  await expect.element(panel.getByTestId("result")).toMatchTextContent(/What\s*video\.mp4\s*Made by\s*video, step 3 of 5/);
  await expect.element(screen.getByTestId("budget")).toHaveTextContent("$5.06 of $40.00");
  await expect.element(screen.getByTestId("budget-line")).toHaveTextContent("$34.94 left before Russ pauses the run.");
  // The step that made the result does not repeat its player below it.
  await expect.element(panel).toMatchTextContent(/It made video\.mp4, the result shown above\./);
  expect(screen.getByTestId("player").elements()).toHaveLength(1);

  await screen.getByRole("button", { name: /^Step 4, review/ }).click();
  await expect.element(screen.getByTestId("decision")).toHaveTextContent('You approved "video.mp4". Your note: Good pace.');

  await screen.getByRole("button", { name: /^Step 5, publish/ }).click();
  expect(screen.getByTestId("result").query()).toBeNull();
  const out = screen.getByTestId("publish");
  await expect.element(out).toMatchTextContent(/Title\s*Kids short\s*Where\s*YouTube\s*Visibility\s*private\s*When.*Views\s*1,234 as of 2026-10-27/);
  await expect.element(out.getByRole("link", { name: "Open the post" })).toHaveAttribute("href", "https://youtu.be/abc");
  await expect.element(screen.getByText("Technical details")).toBeVisible();
  await expect.element(screen.getByTestId("call-request")).not.toBeVisible();
});

test("done with a document: its first lines and Open", async () => {
  const doc = made("stage_0", "notes.md", { body: "---\ntype: file\n---\n# What makes a hook\n\nOpen on the question.\n" });
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(run("done", [research("done")])),
    "GET /api/runs/run_1/artifacts": () => Response.json([doc]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  const result = screen.getByTestId("document");
  await expect.element(result).toMatchTextContent(/What makes a hook\s*Open on the question\.\s*Open$/);
  await expect.element(result.getByRole("link", { name: "Open" })).toHaveAttribute("href", "/api/worlds/world_1/files/runs/run_1/stage_0/artifacts/notes.md");
});

test("done with a list: its item cards", async () => {
  const idea = (title: string, rank: number) =>
    made("stage_0", `idea-${rank}.md`, { type: "idea", title, fields: { rank, hook: `${title} hook` } });
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(run("done", [research("done")])),
    "GET /api/runs/run_1/artifacts": () => Response.json([made("stage_0", "ideas.md"), idea("The chase", 2), idea("Hook first", 1)]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("result")).toMatchTextContent(/^2 ideas from research, step 1 of 1/);
  await expect.element(screen.getByTestId("item").first()).toMatchTextContent(/#1 of 2\s*Hook first\s*Hook first hook/);
});

test("done with audio: a player", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(run("done", [voice("done")])),
    "GET /api/runs/run_1/artifacts": () => Response.json([made("stage_1", "voice.mp3")]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("result").getByTestId("audio")).toHaveAttribute("src", "/api/worlds/world_1/files/runs/run_1/stage_1/artifacts/voice.mp3");
});

test("failed: where it stopped, what was kept, that nothing was posted, the output it printed, and a fork", async () => {
  const error = "ConnectorError: remotion render exited with 1\nBundling 100%\nError: No composition with the ID Shortt found.";
  const failed = run("failed", [research("done"), voice("done"), { ...video("failed"), error }, review("pending"), publish("pending")]);
  let body: unknown = null;
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(failed),
    "POST /api/runs/run_1/fork": async (req) => {
      body = await req.json();
      return Response.json({ ...failed, id: "run_2", status: "queued" }, { status: 201 });
    },
  });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1");
  await expect
    .element(screen.getByTestId("status-line"))
    .toHaveTextContent("Stopped at video: ConnectorError: remotion render exited with 1. Finished stages keep their artifacts. Nothing was posted.");
  await expect.element(screen.getByRole("button", { name: "Step 3, video, failed after 4m 14s" })).toHaveAttribute("aria-pressed", "true");
  await expect.element(screen.getByTestId("stage-log")).toMatchTextContent(/Bundling 100%\s*Error: No composition with the ID Shortt found\./);
  await (await runMenu(screen)).getByRole("menuitem", { name: /^Fork from video/ }).click();
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/worlds/world_1/runs/run_2"));
  expect(body).toEqual({ stage: 2 });
});

test("paused and aborted say where, and an abort names what was kept", async () => {
  const stages = [research("done"), voice("running", timed(2, null)), publish("pending")];
  const posted: string[] = [];
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(run("paused", stages)),
    "POST /api/runs/run_1/resume": () => {
      posted.push("resume");
      return Response.json(run("running", stages));
    },
    "POST /api/runs/run_1/abort": () => Response.json({ detail: "run is done" }, { status: 409 }),
  });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Paused at voice.");
  let menu = await runMenu(screen);
  await expect.element(menu).toMatchTextContent(/^Resume\s*Agents pick up where they stopped; you stay here.*Abort run\s*Stops every agent; finished steps keep their artifacts$/);
  await menu.getByRole("menuitem", { name: /^Resume/ }).click();
  await vi.waitFor(() => expect(posted).toEqual(["resume"]));
  await (await runMenu(screen)).getByRole("menuitem", { name: /^Abort run/ }).click();
  expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Abort Render then pick?"));
  await expect.element(screen.getByRole("alert")).toHaveTextContent("run is done");

  FakeEventSource.emit("run.updated", { payload: run("aborted", [research("done"), voice("failed"), publish("pending")], { updated_at: at(30) }) });
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Aborted at voice. Finished stages keep their artifacts. Nothing was posted.");
  menu = await runMenu(screen);
  await expect.element(menu.getByRole("menuitem", { name: /^Fork from voice/ })).toBeVisible();
  expect(menu.getByRole("menuitem", { name: /^(Pause|Resume|Abort)/ }).elements()).toHaveLength(0);
});

test("clicking a step selects it and keeps it in the URL, and a link opens the same step", async () => {
  fakeFetch({ "GET /api/runs/run_1": () => Response.json(run("done", [research("done"), voice("done"), video("done")])) });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_0");
  await expect.element(screen.getByTestId("stage-panel")).toMatchTextContent(/^research\s*swarm/);
  await screen.getByRole("button", { name: /^Step 2, voice/ }).click();
  await expect.element(screen.getByTestId("stage-panel")).toMatchTextContent(/^voice\s*speech · step 2 of 3 · 1m 14s/);
  expect(router.state.location.search).toEqual({ stage: "stage_1" });
});

test("speech panel: a row per scene with its take, length and engine, and a warning on a join with no silence", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(run("done", [voice("done"), video("done")])),
    "GET /api/runs/run_1/artifacts": () => Response.json([timings, mp4]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_1");
  const speech = screen.getByTestId("stage-panel").getByTestId("speech");
  await expect.element(speech).toMatchTextContent(/^2 scenes · 7\.7 s of narration/);
  const takes = speech.getByTestId("take");
  await expect.element(takes.first()).toMatchTextContent(/hook\s*3\.6 s\s*localhost:8881\s*Line 1 runs into line 2 with no silence to cut at\./);
  expect(takes.first().element().querySelector("audio")?.getAttribute("src")).toBe("/api/worlds/world_1/files/runs/run_1/stage_1/artifacts/narration/hook.wav");
  await expect.element(takes.nth(1)).not.toMatchTextContent(/no silence/);
});

test("speech panel: the voice that spoke, the one chosen when it could not, and the catalog's warnings", async () => {
  const body = JSON.parse(timings.body) as Record<string, unknown>;
  const voiced = { ...timings, body: JSON.stringify({ ...body, voice: "heart", chosen: "narrator", warnings: ["voice 4 in voices.json is ignored: stranger has no consent"] }) };
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(run("done", [voice("done"), video("done")])),
    "GET /api/runs/run_1/artifacts": () => Response.json([voiced, mp4]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_1");
  const speech = screen.getByTestId("stage-panel").getByTestId("speech");
  await expect.element(speech.getByTestId("voice")).toHaveTextContent("voiced by heart");
  await expect.element(speech).toMatchTextContent(/\(chosen narrator, which localhost:8881 cannot speak\)/);
  await expect.element(speech.getByRole("note").first()).toMatchTextContent(/stranger has no consent/);
});

test("build panel: the builder's session in words with its cost, the render's frames live, then the video and its folder", async () => {
  const building = run("running", [voice("done"), video("running", timed(3, null))]);
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(building),
    "GET /api/runs/run_1/agents": () =>
      Response.json([{ id: "a", world_id: "world_1", run_id: "run_1", stage_id: "stage_2", name: "builder", role: "worker", status: "done", session_id: null, cost_usd: 0.8273, created_at: at(3), updated_at: at(5) }]),
    "GET /api/runs/run_1/stages/stage_2/log": () =>
      Response.json([
        log(1, { kind: "session", tool: null, detail: "Session s started on claude-opus-5-5 with skills: a, b" }),
        log(2, { input: { command: "npm install 2>&1 | tail -2" } }),
        log(3, { tool: "Write", input: { file_path: "/data/worlds/world_1/runs/run_1/stage_2/build/src/Main.tsx" } }),
      ]),
    "GET /api/stages/stage_2/call": () => Response.json(call("stage_2")),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  const build = screen.getByTestId("build-log");
  await expect.element(build).toMatchTextContent(/^Writing the project\s*\$0\.8273\s*Installed packages\s*Wrote src\/Main\.tsx$/);
  await expect.element(screen.getByTestId("render-progress")).toHaveTextContent("Rendered 412 of 870 frames");
  await expect.element(screen.getByRole("meter", { name: "Render progress" })).toHaveAttribute("value", "0.4");

  FakeEventSource.emit("connector_call.updated", { payload: call("stage_2", { message: "encoded 778/972 frames", progress: 0.98 }) });
  await expect.element(screen.getByTestId("render-progress")).toHaveTextContent("Encoded 778 of 972 frames");
  await expect.element(screen.getByTestId("project")).toHaveTextContent("/data/worlds/world_1/runs/run_1/stage_2/build");
  expect(screen.getByRole("region", { name: "Live log" }).query()).toBeNull(); // the session is the log
});

test("render panel: progress, then the player once the video is made", async () => {
  const rendering = run("running", [stage(0, "render", "connector", "running", { calls: "remotion_render", ...timed(0, null) }), research("pending")]);
  let artifacts: ArtifactFile[] = [];
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(rendering),
    "GET /api/runs/run_1/artifacts": () => Response.json(artifacts),
    "GET /api/stages/stage_0/call": () => Response.json(call("stage_0")),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("render-progress")).toHaveTextContent("Rendered 412 of 870 frames");
  artifacts = [made("stage_0", "short.mp4", { path: "runs/run_1/stage_0/artifacts/short.mp4.md", type: "video" })];
  FakeEventSource.emit("artifact.created", { payload: { type: "video", path: artifacts[0]!.path, author: "remotion_render", stage_id: "stage_0" } });
  await expect.element(screen.getByTestId("result").getByTestId("player")).toBeVisible();
});

test("retro panel: the proposal and whether it was approved", async () => {
  const proposal = {
    id: "p1",
    world_id: "world_1",
    run_id: "run_1",
    stage_id: "stage_1",
    skill: { action: "new", name: "hooks", content: "", summary: "Open every short on its hook.", evidence: [] },
    workflow: null,
    status: "approved",
    note: "Yes.",
    path: "/x",
    created_at: at(1),
  };
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(run("done", [research("done"), stage(1, "retro", "retro", "done", timed(2, 3))])),
    "GET /api/worlds/world_1/proposals": () => Response.json([{ ...proposal, run_id: "run_other" }, proposal]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_1");
  await expect.element(screen.getByTestId("retro")).toMatchTextContent(/New skill hooks: Open every short on its hook\.\s*You approved it\. Your note: Yes\./);
});

test("a connector stage keeps its raw call under a closed Technical details, and says when it has none yet", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(run("running", [stage(0, "clip", "connector", "running", { calls: "fal_video" })])),
    "GET /api/stages/stage_0/call": () => Response.json(call("stage_0", { connector: "fal_video", tool: "fal_video", request: { output: "clip.mp4" } })),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("stage-panel")).toMatchTextContent(/Working: 40%/);
  await screen.getByText("Technical details").click();
  await expect.element(screen.getByTestId("call")).toMatchTextContent(/fal_video · fal_video · polling · attempt 1 · polled 3 times/);
  await expect.element(screen.getByTestId("call-request")).toMatchTextContent(/"output": "clip\.mp4"/);
});

test("an unknown run explains itself and links back to runs", async () => {
  fakeFetch({});
  const { screen } = await mountAt("/worlds/world_1/runs/run_nope");
  await expect.element(screen.getByRole("heading", { name: "No such run" })).toBeVisible();
  await expect.element(screen.getByRole("link", { name: "Back to runs." })).toHaveAttribute("href", "/worlds/world_1/runs");
});

test("a done run can be saved as a workflow; you stay on the run and a link opens the editor", async () => {
  const done = run("done", [research("done"), review("done")]);
  let body: unknown = null;
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(done),
    "POST /api/runs/run_1/workflow": async (req) => {
      body = await req.json();
      return Response.json({ id: "wfv_7", workflow_id: "wf_7", number: 1, yaml: "name: x\n", created_at: "t", model: { name: "x", stages: [] } }, { status: 201 });
    },
    "GET /api/worlds/world_1": () => Response.json(world),
    "GET /api/worlds": () => Response.json([]),
    "GET /api/workflows/wf_7/versions": () => Response.json([]),
  });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1");
  const menu = await runMenu(screen);
  await expect.element(menu.getByRole("menuitem", { name: /^Save as workflow/ })).toHaveTextContent("Save as workflowSaves the steps it ran as a new workflow; you stay on this run");
  await menu.getByRole("menuitem", { name: /^Save as workflow/ }).click();
  await expect.element(screen.getByTestId("saved")).toHaveTextContent("Saved as a workflow. Open it in the editor");
  expect(body).toEqual({});
  expect(router.state.location.pathname).toBe("/worlds/world_1/runs/run_1");
  await screen.getByRole("link", { name: "Open it in the editor" }).click();
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Workflow");
  expect(router.state.location.pathname).toBe("/worlds/world_1/workflows/wf_7");
});

test("the time strip: widths follow step times, a long wait on a person is capped with its real time, and a segment selects its step", async () => {
  // research 2m 14s, the review waited 50m 14s, video 4m 14s, publish not started.
  const strip = run("running", [research("done"), review("done", timed(2, 52)), video("running", timed(52, 56)), { ...publish("pending"), started_at: null, ended_at: null }]);
  fakeFetch({ "GET /api/runs/run_1": () => Response.json(strip) });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1");
  const seg = (name: RegExp) => screen.getByTestId("strip").getByRole("button", { name });
  await expect.element(seg(/^review/)).toHaveTextContent("review · 50m 14s");
  await expect.element(seg(/^review/)).toHaveAttribute("title", "review · 50m 14s");
  const width = (name: RegExp) => seg(name).element().getBoundingClientRect().width;
  // The wait shows as 1.5 times the longest working step, not 12 times it.
  expect(width(/^review/) / width(/^video/)).toBeCloseTo(1.5, 1);
  expect(width(/^video/) / width(/^research/)).toBeCloseTo(254 / 134, 1);
  expect(width(/^publish · not started/)).toBeCloseTo(56, 0);

  await expect.element(seg(/^video/)).toHaveAttribute("aria-pressed", "true");
  await seg(/^research/).click();
  expect(router.state.location.search).toEqual({ stage: "stage_0" });
  await expect.element(seg(/^research/)).toHaveAttribute("aria-pressed", "true");
  await expect.element(screen.getByRole("button", { name: /^Step 1, research/ })).toHaveAttribute("aria-pressed", "true");
  await expect.element(screen.getByTestId("stage-panel")).toMatchTextContent(/^research/);
});

test("on a phone the selected step comes before the step list", async () => {
  await page.viewport(390, 844);
  fakeFetch({ "GET /api/runs/run_1": () => Response.json(run("running", [research("done"), video("running", timed(3, null))])) });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  const panel = screen.getByTestId("stage-panel");
  await expect.element(panel).toMatchTextContent(/^video/);
  const top = (el: Element) => el.getBoundingClientRect().top;
  expect(top(panel.element())).toBeLessThan(top(screen.getByRole("navigation", { name: "Steps" }).element()));
});

test("on a phone a run with many steps still to come never scrolls sideways", async () => {
  await page.viewport(390, 844);
  // Waiting at step 2 of 9: seven steps not started, each a slot the strip must fit.
  const later = Array.from({ length: 7 }, (_, i) => ({ ...stage(i + 2, `step ${i + 3}`, "connector", "pending"), started_at: null, ended_at: null }));
  const waiting = run("waiting_gate", [research("done"), stage(1, "pick", "gate", "waiting", { ...timed(2, null), ask: "Pick one." }), ...later]);
  fakeFetch({ "GET /api/runs/run_1": () => Response.json(waiting), "GET /api/gates": () => Response.json([]) });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("strip")).toBeVisible();
  const strip = screen.getByTestId("strip").element();
  expect(strip.getBoundingClientRect().right).toBeLessThanOrEqual(390);
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
});

test("a step lists what went in, linked to the step that made it, and every file it made", async () => {
  const script = made("stage_0", "script.md", { body: "# Script" });
  const note = made("stage_2", "notes.md", { type: "note" });
  const later = made("stage_4", "timings.json"); // a later step's file of the same name never counts
  const done = run("done", [
    research("done"),
    { ...voice("done"), inputs: ["script.md"] },
    { ...video("done"), inputs: ["timings.json", "missing.md"] },
    review("done"),
    publish("done"),
  ]);
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(done),
    "GET /api/runs/run_1/artifacts": () => Response.json([script, timings, mp4, note, later]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_2");
  const ins = screen.getByTestId("inputs");
  await expect.element(ins.getByTestId("input").first()).toMatchTextContent(/^timings\.json from step 2, voice$/);
  await expect.element(ins.getByRole("link", { name: "step 2, voice" })).toHaveAttribute("href", "/worlds/world_1/runs/run_1?stage=stage_1");
  await expect.element(ins.getByRole("link", { name: "timings.json" })).toHaveAttribute("href", "/api/worlds/world_1/files/runs/run_1/stage_1/artifacts/timings.json");
  await expect.element(ins.getByTestId("input").nth(1)).toMatchTextContent(/missing\.md not made by an earlier step yet/);
  await screen.getByText("All files (2)").click();
  const files = screen.getByTestId("all-files");
  await expect.element(files.getByRole("link", { name: "video.mp4" })).toHaveAttribute("href", "/api/worlds/world_1/files/runs/run_1/stage_2/artifacts/video.mp4");
  await expect.element(files.getByRole("link", { name: "notes.md" })).toBeVisible();
});

test("an answered review gate shows the video it approved, above the decision", async () => {
  const done = run("done", [research("done"), voice("done"), video("done"), review("done"), publish("done")], {
    decisions: [{ id: "d1", stage_index: 3, stage_name: "review", kind: "approve", choice: "video.mp4", note: "", send_back_to: null, path: "/x", decided_at: at(9), artifact: mp4.path }],
  });
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(done),
    "GET /api/runs/run_1/artifacts": () => Response.json([timings, mp4, posted]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_3");
  const gatePanel = screen.getByTestId("gate-panel");
  await expect.element(gatePanel.getByTestId("under-review").getByTestId("player")).toHaveAttribute("src", "/api/worlds/world_1/files/runs/run_1/stage_2/artifacts/video.mp4");
  expect(screen.getByTestId("inputs").query()).toBeNull();
});
