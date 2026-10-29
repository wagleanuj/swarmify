import { afterEach, beforeEach, expect, test, vi } from "vitest";

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

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("running: the status line says which stage works, the step and the time, and a new run is never an empty rail", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(at(4)));
  const queued = run("queued", [research("pending", timed(0, null)), voice("pending"), video("pending")].map((s) => ({ ...s, started_at: null, ended_at: null })));
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(queued),
    "GET /api/worlds/world_1": () => Response.json(world),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("status-line")).toMatchTextContent(/^Queued\./);
  await expect.element(screen.getByRole("button", { name: "Step 1, research, starting" })).toHaveAttribute("aria-current", "step");
  await expect.element(screen.getByRole("link", { name: "kids videos" })).toHaveAttribute("href", "/worlds/world_1");
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Render then pick v2");
  expect(FakeEventSource.open.map((s) => s.url)).toEqual(["/api/worlds/world_1/events"]);

  const running = run("running", [research("done"), voice("done"), video("running", timed(3, null))], { updated_at: at(4) });
  FakeEventSource.emit("run.updated", { payload: running });
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Video is working · step 3 of 3 · 4 min so far");
  const rail = screen.getByTestId("timeline");
  await expect.element(rail).toMatchTextContent(/research\s*2m 14s\s*voice\s*1m 14s\s*video\s*running/);
  await expect.element(screen.getByRole("button", { name: "Step 3, video, running" })).toHaveAttribute("aria-pressed", "true");
  await expect.element(screen.getByTestId("result")).toHaveTextContent("Nothing made yet. What the run makes shows here as soon as a stage makes it.");

  // An older snapshot replayed from the stream never moves the page back.
  FakeEventSource.emit("run.updated", { payload: { ...queued, updated_at: at(1) } });
  await expect.element(screen.getByTestId("status-line")).toMatchTextContent(/^Video is working/);
  vi.useRealTimers();
});

test("at a gate: the status line asks the gate's question, and Answer now opens the gate on this page", async () => {
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
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  const status = screen.getByTestId("status-line");
  await expect.element(status).toHaveTextContent("Waiting for you: Watch it. Approve or send back.");
  // The thing under review is the result, above every control.
  await expect.element(screen.getByTestId("result").getByTestId("player")).toHaveAttribute("src", "/api/worlds/world_1/files/runs/run_1/stage_2/artifacts/video.mp4");
  await expect.element(screen.getByTestId("gate-panel")).toMatchTextContent(/Watch it\. Approve or send back\.\s*Answer now/);

  await screen.getByRole("button", { name: "Answer now" }).first().click();
  await expect.element(screen.getByTestId("gate-panel")).toMatchTextContent(/Your answer is open at the top of the page\./);
  await screen.getByRole("button", { name: "Approve" }).click();
  await vi.waitFor(() => expect(decided).toMatchObject([{ note: "", artifact: mp4.path }]));
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
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Done in 10 min. Posted private to YouTube.");
  await expect.element(screen.getByRole("link", { name: "Open the post" }).first()).toHaveAttribute("href", "https://youtu.be/abc");
  await expect.element(screen.getByTestId("result")).toMatchTextContent(/What\s*video\.mp4\s*Made by\s*video, step 3 of 5/);
  await expect.element(screen.getByTestId("budget")).toHaveTextContent("$5.06 of $40.00 budget");
  // The default step is the one that made the result; its player is not repeated.
  await expect.element(screen.getByTestId("stage-panel")).toMatchTextContent(/^video\s*build · step 3 of 5 · 4m 14s/);
  await expect.element(screen.getByTestId("stage-panel")).toMatchTextContent(/It made video\.mp4, the result at the top of the page\./);
  expect(screen.getByTestId("player").elements()).toHaveLength(1);

  await screen.getByRole("button", { name: /^Step 4, review/ }).click();
  await expect.element(screen.getByTestId("decision")).toHaveTextContent('You approved "video.mp4". Your note: Good pace.');

  await screen.getByRole("button", { name: /^Step 5, publish/ }).click();
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
  await screen.getByRole("button", { name: "Fork from here" }).click();
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
  await screen.getByRole("button", { name: "Resume" }).click();
  await vi.waitFor(() => expect(posted).toEqual(["resume"]));
  await screen.getByRole("button", { name: "Abort" }).click();
  expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Abort Render then pick?"));
  await expect.element(screen.getByRole("alert")).toHaveTextContent("run is done");

  FakeEventSource.emit("run.updated", { payload: run("aborted", [research("done"), voice("failed"), publish("pending")], { updated_at: at(30) }) });
  await expect.element(screen.getByTestId("status-line")).toHaveTextContent("Aborted at voice. Finished stages keep their artifacts. Nothing was posted.");
  await expect.element(screen.getByRole("button", { name: "Pause" })).not.toBeInTheDocument();
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

test("a done run can be saved as a workflow, which opens in the editor", async () => {
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
  await screen.getByRole("button", { name: "Save as workflow" }).click();
  expect(body).toEqual({});
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Workflow");
  expect(router.state.location.pathname).toBe("/worlds/world_1/workflows/wf_7");
});
