import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { ArtifactFile, Call, Verdict } from "../api/events";
import { CONNECTORS, FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:00:01+00:00";
const researching = run("running", [stage(0, "research", "swarm", "running")]);
const verdict = (loop: number, passed: boolean, over: Partial<Verdict> = {}): Verdict => ({
  id: `verdict_${loop}`,
  world_id: "world_1",
  run_id: "run_1",
  stage_id: "stage_0",
  loop,
  passed,
  reasons: [passed ? "Two ideas, ranked." : "Only one idea."],
  gaps: passed ? [] : ["Find a second idea"],
  items: [],
  path: `runs/run_1/stage_0/artifacts/verdict-${loop}.md`,
  created_at: ts,
  ...over,
});
const idea = (title: string, rank: number, sources: string[] = []): ArtifactFile => ({
  name: `idea-${title.toLowerCase()}.md`,
  path: `runs/run_1/stage_0/artifacts/idea-${title.toLowerCase()}.md`,
  type: "idea",
  author: "judge",
  title,
  fields: { type: "idea", title, rank, sources, hook: `${title} hook`, why: `${title} why` },
  body: `# ${title}`,
  run_id: "run_1",
  stage: "stage_0",
  created: ts,
  pinned: false,
  size: 0,
});

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function routes(verdicts: Verdict[], artifacts: () => ArtifactFile[]) {
  return fakeFetch({
    "GET /api/runs/run_1": () => Response.json(researching),
    "GET /api/runs/run_1/agents": () => Response.json([]),
    "GET /api/runs/run_1/stages/stage_0/log": () => Response.json([]),
    "GET /api/runs/run_1/stages/stage_0/tasks": () => Response.json([]),
    "GET /api/runs/run_1/stages/stage_0/verdicts": () => Response.json(verdicts),
    "GET /api/runs/run_1/artifacts": () => Response.json(artifacts()),
  });
}

test("before the judge runs, the panels say what will appear", async () => {
  routes([], () => []);
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("verdict")).toMatchTextContent(/No verdict yet\. The judge judges once every task/);
  await expect.element(screen.getByTestId("artifacts")).toMatchTextContent(/Nothing yet\. On a pass, the judge turns/);
});

test("a fail shows its gaps, then a streamed pass and its idea cards replace it live", async () => {
  let artifacts: ArtifactFile[] = [];
  routes([verdict(1, false)], () => artifacts);
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  const panel = screen.getByTestId("verdict");
  await expect.element(panel).toMatchTextContent(/fail\s*on loop 1 of 4\. The gap tasks below went back on the whiteboard\.\s*Only one idea\./);
  await expect.element(screen.getByTestId("gaps")).toHaveTextContent("Gap tasks: Find a second idea");

  artifacts = [idea("Chase", 2), idea("Hooks", 1, ["runs/run_1/stage_0/notes/trends.md#p3"])];
  FakeEventSource.emit("verdict.created", { payload: verdict(2, true) });
  FakeEventSource.emit("verdict.created", { payload: verdict(9, false, { stage_id: "stage_9" }) });
  FakeEventSource.emit("artifact.created", {
    payload: { type: "idea", path: artifacts[0]?.path, author: "judge", stage_id: "stage_0" },
  });
  await expect.element(panel).toMatchTextContent(/pass\s*on loop 2 of 4\.Two ideas, ranked\.\s*Loop 1: fail\. Only one idea\.$/);
  const cards = screen.getByTestId("artifact-card");
  await expect.element(cards.first()).toMatchTextContent(/idea · #1 of 2\s*Hooks\s*Hook: Hooks hook\s*Why: Hooks why\s*From trends\.md, paragraph 3/);
  await expect.element(cards.nth(1)).toMatchTextContent(/#2 of 2\s*Chase.*No source paragraph cited\./);
});

test("the fourth fail says the stage failed", async () => {
  routes([1, 2, 3, 4].map((n) => verdict(n, false)), () => []);
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("verdict")).toMatchTextContent(/loop 4 of 4\. That was the last loop, so the stage failed\./);
});

test("a publish card shows its Outcome (one written before outcomes were generic kept views), and a pruned file says its record stayed", async () => {
  const publish: ArtifactFile = {
    name: "publish.md",
    path: "runs/run_1/stage_0/artifacts/publish.md",
    type: "publish",
    author: "publish",
    title: "Five hooks",
    fields: { type: "publish", views: 41250, views_at: "2026-10-27T12:00:00+00:00" },
    body: "# Five hooks",
    run_id: "run_1",
    stage: "stage_0",
    created: ts,
    pinned: false,
    size: 0,
  };
  const video: ArtifactFile = {
    name: "short.mp4",
    path: "runs/run_1/stage_0/artifacts/short.mp4.md",
    type: "file",
    author: "render",
    title: "short.mp4.md",
    fields: { type: "file", deleted: "2027-01-30T00:00:00+00:00" },
    body: "Sidecar of short.mp4.",
    run_id: "run_1",
    stage: "stage_0",
    created: ts,
    pinned: false,
    size: 0,
  };
  routes([], () => [publish, video]);
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("outcome")).toHaveTextContent("41,250 views as of 2026-10-27");
  await expect.element(screen.getByTestId("artifact-card")).not.toHaveTextContent("source paragraph");
  await expect
    .element(screen.getByTestId("artifacts"))
    .toMatchTextContent(/Files: short\.mp4 \(deleted by retention, record kept\)/);
});

const built = (status: string) => run(status === "done" ? "done" : "running", [stage(0, "video", "connector", status, { calls: "remotion_build" })]);
const finish = (status: Call["status"]): Call => ({
  idempotency_key: "stage_0",
  world_id: "world_1",
  run_id: "run_1",
  connector: "remotion_render",
  tool: "remotion_render",
  request: { project: "/data/worlds/world_1/runs/run_1/stage_0/build" },
  status,
  started_at: ts,
  updated_at: ts,
  response: null,
  error: null,
  job_id: "j1",
  poll_tool: "remotion_render_status",
  progress: status === "done" ? 1 : 0.4,
  message: null,
  attempts: 1,
  polls: 1,
  visit: 0,
});

test("a finished build of a Template-capable package saves as a Template by name, replacing one only when asked", async () => {
  const sent: unknown[] = [];
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(built("done")),
    "GET /api/stages/stage_0/call": () => Response.json(finish("done")),
    "POST /api/runs/run_1/stages/stage_0/template": async (req) => {
      const body = (await req.json()) as { name: string; replace?: boolean };
      sent.push(body);
      if (body.name === "cats" && !body.replace) return Response.json({ detail: "cats is already in Remotion templates; replace it or pick a name" }, { status: 409 });
      return Response.json({ name: body.name }, { status: 201 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await screen.getByRole("button", { name: "Save as Template" }).click();
  const name = screen.getByRole("textbox", { name: "Template name" });
  await name.fill("cats");
  await screen.getByRole("button", { name: "Save", exact: true }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("Not saved: cats is already in Remotion templates; replace it or pick a name.");
  await screen.getByRole("checkbox", { name: "Replace cats" }).click();
  await screen.getByRole("button", { name: "Save", exact: true }).click();
  await expect.element(screen.getByText("Saved as the Template cats. It is on the Assets page.")).toHaveAttribute("role", "status");
  expect(sent).toEqual([{ name: "cats", replace: false }, { name: "cats", replace: true }]);
});

test("Save as Template shows only on a finished build of a package that declares Templates", async () => {
  let call = finish("polling");
  let connectors = CONNECTORS;
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(built(call.status === "done" ? "done" : "running")),
    "GET /api/stages/stage_0/call": () => Response.json(call),
    "GET /api/worlds/world_1/connectors": () => Response.json(connectors),
  });
  const first = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(first.screen.getByTestId("build-log")).toBeVisible();
  expect(first.screen.getByRole("button", { name: "Save as Template" }).query()).toBeNull();
  first.screen.unmount();

  call = finish("done");
  connectors = CONNECTORS.map((c) => (c.name === "remotion_build" ? { ...c, capabilities: ["build_session"], templates: "" } : c));
  const second = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(second.screen.getByTestId("project")).toBeVisible();
  expect(second.screen.getByRole("button", { name: "Save as Template" }).query()).toBeNull();
});
