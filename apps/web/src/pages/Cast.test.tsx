import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { answerConfirm, mountAt } from "../test/mount";

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

test("lists the cast as a table by their skin names, each row with its record and model", async () => {
  fakeFetch({
    "GET /api/models": () => Response.json({ default: "claude-opus-5-5", models: ["claude-opus-5-5", "claude-sonnet-5-5"] }),
    "GET /api/worlds/world_1": () => Response.json({ ...video, worker_model: "claude-sonnet-5-5" }),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
  });
  const { screen } = await mountAt("/worlds/world_1/cast");
  await expect.element(screen.getByRole("heading", { name: "Cast", level: 1 })).toBeVisible();
  await expect.element(screen.getByText(/4 agents work in Video.*\$1\.2100 so far/)).toBeVisible();
  const cells = (name: string) =>
    [...screen.getByTestId(`cast-${name}`).element().querySelectorAll("tr:first-child td")].map((td) => td.textContent?.trim());
  await expect.element(screen.getByTestId("cast-worker-1")).toMatchTextContent(/Worker 1/);
  // Agent, then (past the model) tasks, judged, first try, send backs, cost
  const [agent, , ...record] = cells("worker-1");
  expect([agent, ...record.slice(0, 5)]).toEqual([
    "Worker 1claims tasks, writes notes and artifacts · v1, default",
    "3",
    "2",
    "1 of 2",
    "1",
    "$0.7100",
  ]);
  expect(cells("worker-2").slice(2, 7)).toEqual(["0", "0", "-", "0", "$0.0000"]);
  expect(cells("planner")[6]).toBe("$0.5000");
  await expect.element(screen.getByRole("combobox", { name: "Model for Worker 1" })).toHaveDisplayValue("claude-sonnet-5-5");
  await expect.element(screen.getByRole("combobox", { name: "Model for Worker 2" })).toHaveDisplayValue("claude-sonnet-5-5");
  await expect.element(screen.getByRole("combobox", { name: "Model for Planner" })).toHaveDisplayValue("World's: claude-opus-5-5");
  await expect
    .element(screen.getByRole("link", { name: "Change the agent count" }))
    .toHaveAttribute("href", "/worlds/world_1/settings");
});

test("an edit asks for confirmation naming the version, then saves it with a toast and shows the history", async () => {
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
  const { screen } = await mountAt("/worlds/world_1/cast");
  await screen.getByRole("button", { name: "Edit Worker 1" }).click();
  await screen.getByRole("textbox", { name: "Standing instructions" }).fill("Write hooks first.");
  await screen.getByRole("textbox", { name: /Tools/ }).fill("graph, ");
  await expect.element(screen.getByText(/Becomes version 2/)).toBeVisible();
  await screen.getByRole("button", { name: "Save as version 2" }).click();

  expect(await answerConfirm(screen, "Save v2")).toContain("Save version 2 of Worker 1?");
  await vi.waitFor(() => expect(bodies).toEqual([{ prompt: "Write hooks first.", tools: ["graph"], confirm: true }]));
  const row = screen.getByTestId("cast-worker-1");
  await expect.element(screen.getByText("Saved version 2 of Worker 1. It applies from the next stage on.")).toBeVisible(); // a toast
  await expect.element(row).toMatchTextContent(/Worker 1.*v2/);

  await screen.getByRole("button", { name: "History of Worker 1" }).click();
  await expect.element(screen.getByTestId("history")).toMatchTextContent(/v1 default.*no instructions.*v2.*Write hooks first/);
});

test("a cancelled confirmation changes nothing and a refusal is shown", async () => {
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "PUT /api/worlds/world_1/cast/judge": () => Response.json({ detail: "nothing changed" }, { status: 422 }),
  });
  const { screen } = await mountAt("/worlds/world_1/cast");
  await screen.getByRole("button", { name: "Edit Judge" }).click();
  await screen.getByRole("button", { name: "Save as version 2" }).click();
  await answerConfirm(screen, "Cancel");
  expect(calls.filter((c) => c.startsWith("PUT"))).toEqual([]);
  await screen.getByRole("button", { name: "Save as version 2" }).click();
  await answerConfirm(screen, "Save v2");
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

test("a model change saves itself and says so in a toast; the workers share one and the retro has its own", async () => {
  const bodies: unknown[] = [];
  const world = { ...video, model: "claude-sonnet-5-5", judge_model: "claude-opus-5-5" };
  fakeFetch({
    "GET /api/models": () =>
      Response.json({ default: "claude-opus-5-5", models: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"] }),
    "GET /api/worlds/world_1": () => Response.json(world),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "PATCH /api/worlds/world_1": async (req) => {
      const body = (await req.json()) as Record<string, unknown>;
      bodies.push(body);
      return Response.json({ ...world, ...body });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/cast");
  const judge = screen.getByRole("combobox", { name: "Model for Judge" });
  await expect.element(judge).toHaveDisplayValue("claude-opus-5-5");
  await expect.element(screen.getByRole("combobox", { name: "Retro model" })).toHaveDisplayValue("World's: claude-sonnet-5-5");
  await screen.getByRole("combobox", { name: "Model for Worker 1" }).selectOptions("claude-haiku-4-5");
  await expect.element(screen.getByText("Worker 1 now runs on claude-haiku-4-5.")).toBeVisible();
  await expect.element(screen.getByRole("combobox", { name: "Model for Worker 2" })).toHaveDisplayValue("claude-haiku-4-5");
  await judge.selectOptions("World's: claude-sonnet-5-5");
  await expect.element(screen.getByText("Judge now runs on the world's model.")).toBeVisible();
  expect(bodies).toEqual([{ worker_model: "claude-haiku-4-5" }, { judge_model: null }]);
});

test("a skill's history lists its versions and restores an older one as the next version", async () => {
  const bodies: unknown[] = [];
  const hooks = (version: number) => ({
    name: "hooks",
    description: "About hooks.",
    source: "world",
    path: "worlds/world_1/skills/hooks/SKILL.md",
    content: "",
    version,
    run_id: null,
    created_at: video.created_at,
  });
  const version = (number: number, content: string, note = "") => ({
    id: `skill_${number}`,
    world_id: "world_1",
    name: "hooks",
    number,
    content,
    run_id: number === 2 ? "run_01HXABC" : null,
    note,
    retired: false,
    created_at: video.created_at,
  });
  const versions = [version(1, "v1 body"), version(2, "v2 body", "From the retro.")];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "GET /api/worlds/world_1/skills": () => Response.json([hooks(versions.length)]),
    "GET /api/worlds/world_1/skills/hooks/versions": () => Response.json(versions),
    "POST /api/worlds/world_1/skills": async (req) => {
      const body = (await req.json()) as { content: string; note: string };
      bodies.push(body);
      versions.push(version(3, body.content, body.note));
      return Response.json(hooks(3), { status: 201 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/cast");
  await screen.getByRole("button", { name: "History of skill hooks" }).click();
  const history = screen.getByTestId("skill-history");
  await expect.element(history).toMatchTextContent(/v1.*written by you.*v2.*from run 1HXABC.*From the retro\..*current/);
  await expect.element(history.getByRole("button", { name: "Restore v2" })).not.toBeInTheDocument();
  await history.getByRole("button", { name: "Restore v1" }).click();

  expect(await answerConfirm(screen, "Restore")).toContain("Restore v1 of hooks as v3?");
  await vi.waitFor(() => expect(bodies).toEqual([{ name: "hooks", content: "v1 body", note: "Restored v1." }]));
  await expect.element(screen.getByText("Restored v1 of hooks as v3.")).toBeVisible();
  await expect.element(screen.getByTestId("skill-hooks")).toMatchTextContent(/v3/);
  await expect.element(history).toMatchTextContent(/v3.*Restored v1\..*current/);
});

test("while the cast loads its rows are skeletons, not a Loading line", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/cast": () => new Promise<Response>(() => undefined),
    "GET /api/worlds/world_1/skills": () => new Promise<Response>(() => undefined),
  });
  const { screen } = await mountAt("/worlds/world_1/cast");
  await expect.element(screen.getByRole("heading", { name: "Cast", level: 1 })).toBeVisible();
  expect(screen.container.querySelectorAll("[aria-busy='true']")).toHaveLength(2);
  expect(screen.container.querySelectorAll(".cast-skeleton .sk")).toHaveLength(4);
  expect(screen.container.textContent).not.toMatch(/Loading\.\.\.|Loading \w/);
});

test("at 360 wide the cast is a compact list, each row opening the agent's sheet to edit", async () => {
  await page.viewport(360, 740);
  const bodies: unknown[] = [];
  fakeFetch({
    "GET /api/models": () => Response.json({ default: "claude-opus-5-5", models: ["claude-opus-5-5", "claude-sonnet-5-5"] }),
    "GET /api/worlds/world_1": () => Response.json({ ...video, model: "claude-opus-5-5" }),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "PATCH /api/worlds/world_1": async (req) => {
      const body = (await req.json()) as Record<string, unknown>;
      bodies.push(body);
      return Response.json({ ...video, ...body });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/cast");
  const row = screen.getByTestId("cast-worker-1");
  // initial, name over model, cost over the first-try rate
  await expect.element(row).toMatchTextContent(/W\s*Worker 1\s*claude-opus-5-5\s*\$0\.7100\s*50% first try/);
  await expect.element(screen.getByTestId("cast-worker-2")).toMatchTextContent(/not judged yet/);
  const past = [...document.querySelectorAll("main *")].filter((e) => e.getBoundingClientRect().right > 361);
  expect(past.map((e) => `${e.tagName}.${e.className}`)).toEqual([]);
  for (const b of screen.getByTestId("cast").getByRole("button").elements()) expect(b.getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
  expect(screen.getByRole("combobox").elements()).toHaveLength(0); // the pickers wait in the sheets

  await row.getByRole("button").click();
  const sheet = screen.getByRole("dialog", { name: "Worker 1" });
  await expect.element(sheet).toMatchTextContent(/Tasks\s*3\s*Judged\s*2\s*First try\s*1 of 2\s*Send backs\s*1\s*Cost\s*\$0\.7100/);
  await expect.element(sheet.getByRole("textbox", { name: "Standing instructions" })).toBeVisible();
  await sheet.getByRole("combobox", { name: "Model" }).selectOptions("claude-sonnet-5-5");
  await expect.element(screen.getByText("Worker 1 now runs on claude-sonnet-5-5.")).toBeVisible();
  expect(bodies).toEqual([{ worker_model: "claude-sonnet-5-5" }]);
  await sheet.getByRole("button", { name: "Cancel" }).click();
  await expect.element(sheet).not.toBeInTheDocument();
  await page.viewport(1280, 800);
});
