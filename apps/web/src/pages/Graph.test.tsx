import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";

const video = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00" };
const nothing = { rebuilds: 0, seconds: 0, cost_usd: 0 };
const empty = { nodes: 0, edges: 0, god_nodes: [], communities: [], surprises: [], rebuilt: null, failed: null, accepted: false, spent: nothing };
const built = {
  nodes: 1284,
  edges: 3901,
  god_nodes: [
    { label: "Hook pattern", degree: 71 },
    { label: "Retention cliff", degree: 64 },
  ],
  communities: [],
  surprises: [],
  rebuilt: {
    at: new Date().toISOString(),
    nodes: 1284,
    edges: 3901,
    nodes_added: 31,
    edges_added: 58,
    seconds: 12,
    input_tokens: 90000,
    output_tokens: 3000,
    cost_usd: 0.42,
  },
  failed: null,
  accepted: false,
  spent: { rebuilds: 18, seconds: 2700, cost_usd: 12.3 },
};

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("an empty world says how the graph grows, then fills in when a rebuild streams in", async () => {
  let view: object = empty;
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/graph": () => Response.json(view),
  });
  const { screen } = await mountAt("/worlds/world_1/graph");
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Memory");
  await expect.element(screen.getByTestId("graph-empty")).toMatchTextContent(/The graph writer folds them in/);
  await expect.element(screen.getByTestId("rebuilt")).toHaveTextContent("Not built yet");

  // Beside the shell's one all-worlds stream.
  expect(FakeEventSource.open.map((s) => s.url).sort()).toEqual(["/api/events?since=now", "/api/worlds/world_1/events?since=now"]);
  view = built;
  FakeEventSource.emit("graph.rebuilt", { payload: built.rebuilt });
  await expect
    .element(screen.getByTestId("graph-size"))
    .toMatchTextContent(/Nodes\s*1,284\s*Edges\s*3,901\s*Rebuilds\s*18\s*Spent on memory\s*\$12\.30/);
  await expect
    .element(screen.getByTestId("rebuilt"))
    .toHaveTextContent("The graph writer rebuilt it just now: +31 nodes, +58 edges in 12s, est. $0.42");
  await expect.element(screen.getByTestId("spent")).toHaveAttribute("title", "45m of extraction");
  // The most connected notes, ranked by their links.
  await expect.element(screen.getByTestId("god-nodes")).toMatchTextContent(/^Hook pattern\s*71\s*Retention cliff\s*64$/);
  expect(screen.getByTestId("graph-empty").elements()).toHaveLength(0);
});

test("the query box and a god node ask the graph and show its answer", async () => {
  const asked: unknown[] = [];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/graph": () => Response.json(built),
    "POST /api/worlds/world_1/graph/query": async (req) => {
      const body = (await req.json()) as { question: string };
      asked.push(body);
      return Response.json({ answer: `NODE ${body.question}` });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/graph");
  await screen.getByLabelText("Ask the graph").fill("what beats the cliff?");
  await screen.getByRole("button", { name: "Ask", exact: true }).click();
  await expect.element(screen.getByTestId("answer")).toMatchTextContent(/NODE what beats the cliff\?/);

  await screen.getByRole("button", { name: "Retention cliff" }).click();
  await expect
    .element(screen.getByTestId("answer"))
    .toMatchTextContent(/NODE What do we know about Retention cliff\?/);
  expect(asked).toEqual([
    { question: "what beats the cliff?" },
    { question: "What do we know about Retention cliff?" },
  ]);
});

test("a failed rebuild is shown with its reason", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/graph": () =>
      Response.json({ ...built, failed: { at: new Date().toISOString(), error: "claude is not logged in" } }),
  });
  const { screen } = await mountAt("/worlds/world_1/graph");
  await expect
    .element(screen.getByTestId("failed"))
    .toMatchTextContent(/The graph writer's last rebuild failed just now: claude is not logged in\./);
});

test("a shrink the guard refused is shown with one click to accept it", async () => {
  const shrink = {
    at: new Date().toISOString(),
    error: "unverified semantic shrink for 'remotion-scaffold.md' (6 -> 5 nodes)",
    shrink: true,
    seconds: 140,
  };
  let view: object = { ...built, failed: shrink };
  const accepted: string[] = [];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/graph": () => Response.json(view),
    "POST /api/worlds/world_1/graph/accept-shrink": () => {
      accepted.push("world_1");
      view = { ...built, failed: shrink, accepted: true };
      return new Response(null, { status: 202 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/graph");
  await expect
    .element(screen.getByTestId("failed"))
    .toMatchTextContent(/graphify refused to write a smaller graph/);
  await screen.getByRole("button", { name: "Accept the shrink" }).click();
  await expect.element(screen.getByTestId("failed")).toMatchTextContent(/Rebuilding with the shrink accepted/);
  expect(accepted).toEqual(["world_1"]);
  expect(screen.getByRole("button", { name: "Accept the shrink" }).elements()).toHaveLength(0);
});

test("communities with their size and surprising links name each node with its run and note", async () => {
  const note = "runs/run_1/research/notes/hooks.md";
  const hooks = { label: "Short hooks", source_file: note, run_id: "run_1" };
  const brief = { label: "Brief", source_file: "brief.md", run_id: null };
  const asked: unknown[] = [];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/graph": () =>
      Response.json({
        ...built,
        communities: [{ id: 0, name: "Hooks", size: 12, nodes: [hooks, brief] }],
        surprises: [{ source: hooks, target: brief, relation: "contradicts", why: "crosses communities" }],
      }),
    "POST /api/worlds/world_1/graph/query": async (req) => {
      asked.push(await req.json());
      return Response.json({ answer: "ok" });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/graph");
  const communities = screen.getByTestId("communities");
  await expect.element(communities).toMatchTextContent(/^Hooks\s*Short hooks run note, Brief note\s*12$/);
  const runs = communities.getByRole("link", { name: "run" });
  await expect.element(runs).toHaveAttribute("href", "/worlds/world_1/runs/run_1");
  const notes = communities.getByRole("link", { name: "note" });
  expect(notes.elements().map((a) => a.getAttribute("href"))).toEqual([
    "/worlds/world_1/read?path=runs%2Frun_1%2Fresearch%2Fnotes%2Fhooks.md",
    "/worlds/world_1/read?path=brief.md",
  ]);
  await expect
    .element(screen.getByTestId("surprises"))
    .toMatchTextContent(/Short hooks.*contradicts.*Brief.*crosses communities/);
  await communities.getByRole("button", { name: "Brief" }).click();
  await expect.poll(() => asked).toEqual([{ question: "What do we know about Brief?" }]);
});

test("a graph with no communities yet says so", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/graph": () => Response.json(built),
  });
  const { screen } = await mountAt("/worlds/world_1/graph");
  await expect.element(screen.getByTestId("no-communities")).toBeVisible();
});
