import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";

const video = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00" };
const empty = { nodes: 0, edges: 0, god_nodes: [], rebuilt: null, failed: null };
const built = {
  nodes: 1284,
  edges: 3901,
  god_nodes: [
    { label: "Hook pattern", degree: 71 },
    { label: "Retention cliff", degree: 64 },
  ],
  rebuilt: { at: new Date().toISOString(), nodes: 1284, edges: 3901, nodes_added: 31, edges_added: 58, seconds: 12 },
  failed: null,
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
  await expect.element(screen.getByTestId("graph-empty")).toMatchTextContent(/Anton folds them in/);
  await expect.element(screen.getByTestId("rebuilt")).toHaveTextContent("Not built yet");

  expect(FakeEventSource.open.map((s) => s.url)).toEqual(["/api/worlds/world_1/events?since=now"]);
  view = built;
  FakeEventSource.emit("graph.rebuilt", { payload: built.rebuilt });
  await expect.element(screen.getByTestId("graph-size")).toMatchTextContent(/1,284 nodes\s*3,901 edges/);
  await expect
    .element(screen.getByTestId("rebuilt"))
    .toHaveTextContent("Anton rebuilt it just now: +31 nodes, +58 edges in 12s");
  await expect.element(screen.getByTestId("god-nodes")).toMatchTextContent(/Hook pattern\s*71 links/);
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
    .toMatchTextContent(/Anton's last rebuild failed just now: claude is not logged in\./);
});
