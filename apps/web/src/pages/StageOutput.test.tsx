import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { ArtifactFile, Verdict } from "../api/events";
import { FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
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
    "GET /api/runs/run_1/stages/stage_0/artifacts": () => Response.json(artifacts()),
  });
}

test("before the judge runs, the panels say what will appear", async () => {
  routes([], () => []);
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("verdict")).toMatchTextContent(/No verdict yet\. Erlich judges once every task/);
  await expect.element(screen.getByTestId("artifacts")).toMatchTextContent(/Nothing yet\. On a pass, Erlich turns/);
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

test("a publish card shows the views written back, and a pruned file says its record stayed", async () => {
  const publish: ArtifactFile = {
    name: "publish.md",
    path: "runs/run_1/stage_0/artifacts/publish.md",
    type: "publish",
    author: "publish",
    title: "Five hooks",
    fields: { type: "publish", views: 41250, views_at: "2026-10-27T12:00:00+00:00" },
    body: "# Five hooks",
  };
  const video: ArtifactFile = {
    name: "short.mp4",
    path: "runs/run_1/stage_0/artifacts/short.mp4.md",
    type: "file",
    author: "render",
    title: "short.mp4.md",
    fields: { type: "file", deleted: "2027-01-30T00:00:00+00:00" },
    body: "Sidecar of short.mp4.",
  };
  routes([], () => [publish, video]);
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("views")).toHaveTextContent("41,250 views as of 2026-10-27");
  await expect.element(screen.getByTestId("artifact-card")).not.toHaveTextContent("source paragraph");
  await expect
    .element(screen.getByTestId("artifacts"))
    .toMatchTextContent(/Files: short\.mp4 \(deleted by retention, record kept\)/);
});
