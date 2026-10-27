import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";

const video = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00" };
const empty = { tasks_done: 0, judged: 0, first_try: 0, send_backs: 0, cost_usd: 0 };
const member = (name: string, role: string, over = {}) => ({
  name,
  role,
  version: 1,
  prompt: "",
  tools: null,
  updated_at: null,
  record: empty,
  ...over,
});
const cast = [
  member("planner", "planner", { record: { ...empty, cost_usd: 0.5 } }),
  member("worker-1", "worker", {
    record: { tasks_done: 3, judged: 2, first_try: 1, send_backs: 1, cost_usd: 0.71 },
  }),
  member("worker-2", "worker"),
  member("judge", "judge"),
];

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("lists the cast by their skin names with each record beside its reference", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
  });
  const { screen } = await mountAt("/worlds/world_1/cast");
  await expect.element(screen.getByRole("heading", { name: "Cast", level: 1 })).toBeVisible();
  await expect.element(screen.getByRole("heading", { name: /Richard/ })).toBeVisible();
  await expect.element(screen.getByRole("heading", { name: /Erlich/ })).toBeVisible();
  await expect
    .element(screen.getByTestId("cast-worker-1"))
    .toMatchTextContent(/Gilfoyle.*v1, default.*3 tasks done · 1 of 2 stages passed first try · 1 send-back · \$0\.7100 est\./);
  await expect.element(screen.getByTestId("cast-worker-2")).toMatchTextContent(/Dinesh.*No record yet/);
  await expect.element(screen.getByTestId("cast-planner")).toMatchTextContent(/Estimated cost so far: \$0\.5000/);
  await expect.element(screen.getByTestId("cast-judge")).toMatchTextContent(/No sessions yet/);
  await expect
    .element(screen.getByRole("link", { name: "Change the agent count" }))
    .toHaveAttribute("href", "/worlds/world_1/settings");
});

test("an edit asks for confirmation naming the version, then saves it and shows the history", async () => {
  const bodies: unknown[] = [];
  const edited = member("worker-1", "worker", {
    version: 2,
    prompt: "Write hooks first.",
    tools: ["graph"],
    updated_at: video.created_at,
  });
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "PUT /api/worlds/world_1/cast/worker-1": async (req) => {
      bodies.push(await req.json());
      return Response.json(edited);
    },
    "GET /api/worlds/world_1/cast/worker-1/versions": () =>
      Response.json([
        { name: "worker-1", number: 1, prompt: "", tools: null, created_at: video.created_at },
        { name: "worker-1", number: 2, prompt: "Write hooks first.", tools: ["graph"], created_at: video.created_at },
      ]),
  });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  const { screen } = await mountAt("/worlds/world_1/cast");
  await screen.getByRole("button", { name: "Edit Gilfoyle" }).click();
  await screen.getByRole("textbox", { name: "Standing instructions" }).fill("Write hooks first.");
  await screen.getByRole("textbox", { name: /Tools/ }).fill("graph, ");
  await expect.element(screen.getByText(/Becomes version 2/)).toBeVisible();
  await screen.getByRole("button", { name: "Save as version 2" }).click();

  expect(confirm.mock.calls[0]?.[0]).toContain("Save version 2 of Gilfoyle?");
  expect(bodies).toEqual([{ prompt: "Write hooks first.", tools: ["graph"], confirm: true }]);
  await expect.element(screen.getByRole("status")).toMatchTextContent(/Saved version 2 of Gilfoyle/);
  await expect
    .element(screen.getByTestId("cast-worker-1"))
    .toMatchTextContent(/v2.*Standing instructions: Write hooks first\..*Tools: graph/);

  await screen.getByRole("button", { name: "History of Gilfoyle" }).click();
  await expect.element(screen.getByTestId("history")).toMatchTextContent(/v1 default.*no instructions.*v2.*Write hooks first/);
});

test("a cancelled confirmation changes nothing and a refusal is shown", async () => {
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "PUT /api/worlds/world_1/cast/judge": () => Response.json({ detail: "nothing changed" }, { status: 422 }),
  });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const { screen } = await mountAt("/worlds/world_1/cast");
  await screen.getByRole("button", { name: "Edit Erlich" }).click();
  await screen.getByRole("button", { name: "Save as version 2" }).click();
  expect(calls.filter((c) => c.startsWith("PUT"))).toEqual([]);
  confirm.mockReturnValue(true);
  await screen.getByRole("button", { name: "Save as version 2" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("Not saved: nothing changed.");
});

test("lists the skills the cast loads, each with where it came from", async () => {
  const skill = (name: string, over = {}) => ({
    name,
    description: `About ${name}.`,
    source: "world",
    path: `worlds/world_1/skills/${name}/SKILL.md`,
    content: "",
    version: null,
    run_id: null,
    created_at: null,
    ...over,
  });
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "GET /api/worlds/world_1/skills": () =>
      Response.json([
        skill("cite", { source: "shared", path: "skills/cite/SKILL.md" }),
        skill("hand"),
        skill("hooks", { version: 2, run_id: "run_01HXABC", created_at: video.created_at }),
      ]),
  });
  const { screen } = await mountAt("/worlds/world_1/cast");
  await expect.element(screen.getByRole("heading", { name: "Skills" })).toBeVisible();
  await expect.element(screen.getByTestId("skill-cite")).toMatchTextContent(/cite.*shared.*About cite\..*every world reads it/);
  await expect.element(screen.getByTestId("skill-hand")).toMatchTextContent(/this world.*Hand-written/);
  await expect.element(screen.getByTestId("skill-hooks")).toMatchTextContent(/this world.*v2 · from run 1HXABC/);
  await expect
    .element(screen.getByTestId("skill-hooks").getByRole("link"))
    .toHaveAttribute("href", "/worlds/world_1/runs/run_01HXABC");
});

test("with no skills it says where the first one comes from", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "GET /api/worlds/world_1/skills": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/cast");
  await expect.element(screen.getByTestId("no-skills")).toMatchTextContent(/approve a retro proposal or promote/);
});
