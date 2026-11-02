import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import { FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:02:00+00:00";
const render = stage(1, "render", "connector", "pending", { calls: "remotion_render", config: { calls: "remotion_render", params: { fps: 30 } } });
const waiting = run("waiting_gate", [stage(0, "pick", "gate", "waiting", { started_at: ts }), render], { revision: 3 });
const after = { calls: "remotion_render", params: { fps: 60 } };
const proposal = (revision: number) => ({
  change: { kind: "stage_edit", stage: "stage_1", payload: after },
  stage_name: "render",
  before: render.config,
  after,
  violations: [],
  revision,
  in_place: true,
});
const asked = { id: "message_1", role: "operator", text: "Render at 60 fps.", at: ts, sources: [], proposals: [], cost_usd: 0 };
const answered = (revision: number, state = "open") => ({
  id: "message_2",
  role: "assistant",
  text: "The render stage runs at 30 fps; here is the edit.",
  at: ts,
  sources: [{ label: "from render's config", stage: "stage_1" }],
  proposals: [{ id: "proposal_1", state, proposal: proposal(revision), change_id: null }],
  cost_usd: 0.02,
});

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("asking from the run header answers with source links and a change card that applies through the Run Change seam", async () => {
  await page.viewport(1280, 800);
  let conversation: unknown = { messages: [], spent_usd: 0 };
  const applied: unknown[] = [];
  const marked: unknown[] = [];
  const calls = fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "GET /api/runs/run_1/assistant": () => Response.json(conversation),
    "POST /api/runs/run_1/assistant": async (req) => {
      expect(await req.json()).toEqual({ message: "Render at 60 fps." });
      conversation = { messages: [asked, answered(3)], spent_usd: 0.02 };
      return Response.json(answered(3));
    },
    "POST /api/runs/run_1/changes": async (req) => {
      const body = await req.json();
      applied.push(body);
      return Response.json(
        { id: "change_1", run_id: "run_1", kind: "stage_edit", stage_id: "stage_1", stage_index: 1, stage_name: "render", payload: after, before: render.config, by: "operator", at: ts, revision: 4 },
        { status: 201 },
      );
    },
    "POST /api/runs/run_1/assistant/proposals/proposal_1": async (req) => {
      const body = await req.json();
      marked.push(body);
      return Response.json({ id: "proposal_1", state: body.state, proposal: proposal(3), change_id: body.change_id });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_0");
  await screen.getByRole("button", { name: "Ask this run" }).click();

  const panel = screen.getByTestId("assistant");
  await expect.element(panel.getByRole("heading", { name: "Ask this run" })).toBeVisible();
  await expect.element(panel).toMatchTextContent(/It reads the run and proposes changes\. Nothing changes until you apply\./);
  await expect.element(panel).toMatchTextContent(/Asking costs about \$0\.02 a question, from the world's budget\./);
  const field = panel.getByPlaceholder("Ask about this run, or say what to change");
  await field.fill("Render at 60 fps.");
  await panel.getByRole("button", { name: "Send" }).click();

  await expect.element(panel).toMatchTextContent(/The render stage runs at 30 fps; here is the edit\./);
  const source = panel.getByRole("link", { name: "from render's config" });
  await expect.element(source).toHaveAttribute("href", "/worlds/world_1/runs/run_1?stage=stage_1");
  const card = panel.getByTestId("change-card");
  await expect.element(card.getByRole("heading", { name: "Edit render" })).toBeVisible();
  await expect.element(card.getByRole("list", { name: "The change" })).toHaveTextContent('params {"fps":30} {"fps":60}');
  expect(calls).not.toContain("POST /api/runs/run_1/changes/propose"); // shown as proposed

  await card.getByRole("button", { name: "Apply" }).click();
  await expect.element(card).toMatchTextContent(/Applied by you at \d\d:\d\d/);
  expect(applied).toEqual([{ kind: "stage_edit", stage: "stage_1", payload: after, revision: 3 }]);
  await vi.waitFor(() => expect(marked).toEqual([{ state: "applied", change_id: "change_1" }]));
  await expect.element(screen.getByTestId("assistant-spend")).toHaveTextContent("Assistant $0.02, from the world's budget, not this run's.");
});

test("the conversation comes back on reload; a stale card asks again and Not now dismisses it", async () => {
  await page.viewport(1280, 800);
  const marked: unknown[] = [];
  const calls = fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "GET /api/runs/run_1/assistant": () => Response.json({ messages: [asked, answered(2, "stale")], spent_usd: 0.02 }),
    "POST /api/runs/run_1/changes/propose": () => Response.json(proposal(3)),
    "POST /api/runs/run_1/assistant/proposals/proposal_1": async (req) => {
      const body = await req.json();
      marked.push(body);
      return Response.json({ id: "proposal_1", state: body.state, proposal: proposal(2), change_id: null });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_0");
  await screen.getByRole("button", { name: "Ask this run" }).click();
  const panel = screen.getByTestId("assistant");
  await expect.element(panel).toMatchTextContent(/Render at 60 fps\./);
  const card = panel.getByTestId("change-card");
  await expect.element(card).toMatchTextContent(/The run moved on since this was proposed\./);
  await expect.element(card.getByRole("button", { name: "Apply" })).toBeDisabled();
  await card.getByRole("button", { name: "Ask again" }).click();
  await expect.element(card).not.toMatchTextContent(/The run moved on/);
  expect(calls).toContain("POST /api/runs/run_1/changes/propose");

  await card.getByRole("button", { name: "Not now" }).click();
  await expect.element(panel.getByTestId("change-card")).not.toBeInTheDocument();
  expect(marked).toEqual([{ state: "dismissed", change_id: null }]);
  expect(calls).not.toContain("POST /api/runs/run_1/changes");
});

test("on a phone the assistant opens as a sheet from the run header", async () => {
  await page.viewport(390, 844);
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json({ ...waiting, changes: [{ id: "change_1", run_id: "run_1", kind: "stage_edit", stage_id: "stage_1", stage_index: 1, stage_name: "render", payload: after, before: {}, by: "operator", at: ts, revision: 4 }] }),
    "GET /api/runs/run_1/assistant": () => {
      const done = { ...answered(3, "applied"), proposals: [{ ...answered(3).proposals[0], state: "applied", change_id: "change_1" }] };
      return Response.json({ messages: [asked, done], spent_usd: 0.02 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_0");
  await screen.getByRole("button", { name: "Ask this run" }).click();
  const sheet = screen.getByRole("dialog", { name: "Ask this run" });
  await expect.element(sheet).toBeVisible();
  await expect.element(sheet).toMatchTextContent(/The render stage runs at 30 fps/);
  await expect.element(sheet).toMatchTextContent(/Edit render\. Applied by you at \d\d:\d\d/);
  await expect.element(sheet.getByPlaceholder("Ask about this run, or say what to change")).toBeVisible();
  await expect.element(sheet.getByRole("button", { name: "Send" })).toBeVisible();
  expect(screen.getByTestId("assistant").elements()).toHaveLength(1); // the sheet's, no column
});
