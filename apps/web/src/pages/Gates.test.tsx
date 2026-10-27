import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, gate, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:00:00+00:00";
const video = { id: "world_1", name: "Video", purpose: "", created_at: ts };
const inbox = [
  gate("confirm", { run_id: "run_3", stage_id: "s3", run_name: "Publish it" }),
  gate("approve", { run_id: "run_2", stage_id: "s2", send_backs_left: 1, earlier_stages: ["research", "script"] }),
  gate("pick", { run_id: "run_1", stage_id: "s1" }),
];

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("renders each gate kind with its own control and posts the decision", async () => {
  const bodies: Record<string, unknown> = {};
  const decision = (id: string) => async (req: Request) => {
    bodies[id] = await req.json();
    return Response.json(run("running", [stage(0, "x", "connector", "running")], { id }));
  };
  const calls = fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json(inbox),
    "POST /api/runs/run_1/decision": decision("run_1"),
    "POST /api/runs/run_2/decision": decision("run_2"),
    "POST /api/runs/run_3/decision": decision("run_3"),
  });
  const { screen } = await mountAt("/gates");
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Needs you");
  const list = screen.getByTestId("gate-list");
  await expect.element(list).toMatchTextContent(/Publish it.*Weekly short.*Weekly short/);
  expect(FakeEventSource.open.map((s) => s.url)).toEqual(["/api/worlds/world_1/events"]);

  const pick = screen.getByTestId("gate-run_1");
  await expect.element(pick).toMatchTextContent(/Pick one idea\./);
  await pick.getByLabelText("Your pick").fill("idea 2");
  await pick.getByLabelText("Note (optional)").fill("Less text on screen.");
  await pick.getByRole("button", { name: "Continue with this pick" }).click();
  await vi.waitFor(() => expect(bodies.run_1).toEqual({ choice: "idea 2", note: "Less text on screen." }));

  const approve = screen.getByTestId("gate-run_2");
  await expect.element(approve).toMatchTextContent(/1 of 2 send-backs left/);
  await approve.getByRole("button", { name: "Send back..." }).click();
  await expect.element(approve.getByLabelText(/Send back to/)).toHaveValue("script");
  await approve.getByLabelText(/Send back to/).selectOptions("research");
  await approve.getByLabelText(/^Note/).fill("Too much text.");
  await approve.getByRole("button", { name: "Send back" }).click();
  await vi.waitFor(() =>
    expect(bodies.run_2).toEqual({ note: "Too much text.", send_back_to: "research" }),
  );

  const confirm = screen.getByTestId("gate-run_3");
  await expect.element(confirm).toMatchTextContent(/Nothing goes out until you confirm\./);
  await confirm.getByRole("button", { name: "Confirm" }).click();
  await vi.waitFor(() => expect(bodies.run_3).toEqual({ note: "" }));
  // Each answer hides its card at once, without refetching before the run moves.
  await expect.element(screen.getByTestId("no-gates")).toBeInTheDocument();
  expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(1);
});

test("approve without send-backs left says so, and a refused decision is an alert", async () => {
  const calls = fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json([gate("approve", { send_backs_left: 0 })]),
    "POST /api/runs/run_1/decision": () =>
      Response.json({ detail: "already sent back 2 times; approve or abort" }, { status: 409 }),
  });
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_1");
  await expect.element(card).toMatchTextContent(/Sent back twice already/);
  await expect.element(card.getByRole("button", { name: "Send back..." })).not.toBeInTheDocument();
  await card.getByRole("button", { name: "Approve" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("already sent back 2 times; approve or abort");
  await vi.waitFor(() => expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(2));
});

test("the inbox refetches when a run moves, and says what it is for when empty", async () => {
  let open = inbox;
  const calls = fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json(open),
  });
  const { screen } = await mountAt("/gates");
  await expect.element(screen.getByTestId("gate-list")).toMatchTextContent(/Publish it/);
  open = [];
  FakeEventSource.emit("run.updated", { payload: run("done", []) });
  await expect.element(screen.getByTestId("no-gates")).toMatchTextContent(/Nothing is waiting on you/);
  expect(calls.filter((c) => c === "GET /api/gates").length).toBeGreaterThanOrEqual(2);
  await expect
    .element(screen.getByRole("link", { name: "See your worlds" }))
    .toHaveAttribute("href", "/");
});

test("a gate before a publish stage shows exactly what goes out, and refuses when it cannot", async () => {
  const preview = {
    connector: "youtube",
    title: "Five hooks that work",
    description: "",
    visibility: "public",
    schedule: "2099-01-01T09:00:00Z",
    video: "short.mp4",
    platforms: [],
    problems: [],
  };
  const bodies: unknown[] = [];
  const calls = fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () =>
      Response.json([
        gate("confirm", { publish: preview }),
        gate("confirm", {
          run_id: "run_2",
          stage_id: "s2",
          publish: { ...preview, connector: "upload_post", schedule: null, platforms: ["tiktok"], problems: ["upload_post needs params.user, the Upload-Post profile to post as"] },
        }),
      ]),
    "POST /api/runs/run_1/decision": async (req) => {
      bodies.push(await req.json());
      return Response.json(run("running", []));
    },
  });
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_1");
  const out = card.getByRole("region", { name: "What goes out" });
  await expect.element(out).toMatchTextContent(/Title\s*Five hooks that work/);
  await expect.element(out).toMatchTextContent(/Where\s*YouTube/);
  await expect.element(out).toMatchTextContent(/Visibility\s*public/);
  await expect.element(out).toMatchTextContent(/2099-01-01 09:00 UTC\)\. Private until then, then public\./);
  await expect.element(out).toMatchTextContent(/Video\s*short\.mp4/);
  await expect.element(out.getByRole("time")).toHaveAttribute("datetime", "2099-01-01T09:00:00Z");
  await expect.element(card).toMatchTextContent(/Posts exactly what is above\./);
  await card.getByRole("button", { name: "Confirm and post" }).click();
  await vi.waitFor(() => expect(bodies).toEqual([{ note: "" }]));

  const broken = screen.getByTestId("gate-run_2");
  await expect.element(broken).toMatchTextContent(/Upload-Post to tiktok/);
  await expect.element(broken).toMatchTextContent(/Now, as soon as you confirm/);
  await expect.element(broken.getByRole("alert")).toMatchTextContent(/This will not post:.*needs params\.user/);
  await expect.element(broken.getByRole("button", { name: "Confirm and post" })).toBeDisabled();
  await expect.element(card).not.toBeInTheDocument();
  expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(1);
});

const hooks = "---\nname: hooks\ndescription: Open on the hook.\n---\n\nWrite the hook first.\n";
const proposal = {
  id: "prop_1",
  world_id: "world_1",
  run_id: "run_4",
  stage_id: "s4",
  skill: {
    action: "new",
    name: "hooks",
    content: hooks,
    summary: "Hook first",
    evidence: ["review: sent back with the note: Hook sooner."],
  },
  workflow: {
    summary: "Ask for hooks up front",
    yaml: "name: Research\nstages: []\n",
    evidence: ["send_back at review: research in 3 runs"],
  },
  status: "pending",
  note: "",
  path: "runs/run_4/s4/artifacts/proposal.md",
  created_at: ts,
  decided_at: null,
};

test("a skill gate shows the proposal with its evidence and posts the edited skill on approval", async () => {
  const bodies: unknown[] = [];
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json([gate("skill", { run_id: "run_4", stage_id: "s4", stage_name: "retro", earlier_stages: ["research", "review"] })]),
    "GET /api/runs/run_4/proposal": () => Response.json(proposal),
    "POST /api/proposals/prop_1/decide": async (req) => {
      bodies.push(await req.json());
      return Response.json({ ...proposal, status: "approved" });
    },
  });
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_4");
  await expect.element(card).toMatchTextContent(/Approve it, edit it, or reject it/);
  await expect.element(card.getByTestId("skill-change")).toMatchTextContent(/New skill hooks.*Hook first.*sent back with the note: Hook sooner/);
  await expect.element(card.getByTestId("workflow-change")).toMatchTextContent(/Ask for hooks up front.*in 3 runs.*name: Research/);
  await expect.element(card.getByRole("button", { name: "Send back..." })).not.toBeInTheDocument();
  const edited = hooks.replace("Write the hook first.", "Write the hook in the first second.");
  await card.getByLabelText(/SKILL\.md/).fill(edited);
  await card.getByLabelText(/Save it as the workflow's next version/).click();
  await card.getByLabelText(/^Note/).fill("Tightened.");
  await card.getByRole("button", { name: "Approve" }).click();
  await vi.waitFor(() =>
    expect(bodies).toEqual([{ approve: true, content: edited, apply_workflow: false, note: "Tightened." }]),
  );
  await expect.element(screen.getByTestId("no-gates")).toBeVisible();
});

test("rejecting a skill proposal asks for the reason and records it", async () => {
  const bodies: unknown[] = [];
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json([gate("skill", { run_id: "run_4", stage_id: "s4", stage_name: "retro" })]),
    "GET /api/runs/run_4/proposal": () => Response.json({ ...proposal, workflow: null }),
    "POST /api/proposals/prop_1/decide": async (req) => {
      bodies.push(await req.json());
      return Response.json({ detail: "the proposal was already approved" }, { status: 422 });
    },
  });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_4");
  await expect.element(card.getByTestId("skill-change")).toBeVisible();
  await card.getByRole("button", { name: "Reject" }).click();
  expect(confirm).toHaveBeenCalledOnce();
  expect(bodies).toEqual([]);
  await card.getByLabelText(/^Note/).fill("Not the gap.");
  await card.getByRole("button", { name: "Reject" }).click();
  await vi.waitFor(() =>
    expect(bodies).toEqual([{ approve: false, content: hooks, apply_workflow: false, note: "Not the gap." }]),
  );
  await expect.element(screen.getByRole("alert")).toHaveTextContent("the proposal was already approved");
});

const idea = (name: string, title: string, fields: Record<string, unknown>) => ({
  name: `idea-${name}.md`,
  path: `runs/run_1/stage_0/artifacts/idea-${name}.md`,
  type: "idea",
  author: "judge",
  title,
  fields,
  body: `# ${title}\n\nOpen on it.`,
  run_id: "run_1",
  stage: "stage_0",
  created: ts,
  pinned: false,
});
const short = {
  name: "short.mp4",
  path: "runs/run_2/stage_2/artifacts/short.mp4.md",
  type: "video",
  author: "connector",
  title: "short.mp4",
  fields: { file: "short.mp4" },
  body: "# short.mp4\n",
  run_id: "run_2",
  stage: "stage_2",
  created: ts,
  pinned: false,
};

test("a pick gate offers the earlier stage's cards and an approve gate plays the piece in place", async () => {
  const bodies: Record<string, unknown> = {};
  const queries: string[] = [];
  const decision = (id: string) => async (req: Request) => {
    bodies[id] = await req.json();
    return Response.json(run("running", [], { id }));
  };
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json([inbox[1]!, inbox[2]!]),
    "GET /api/runs/run_1/artifacts": (req) => {
      queries.push(new URL(req.url).search);
      return Response.json([
        idea("chase", "The chase", { rank: 2, sources: [] }),
        idea("hooks", "Hook first", { rank: 1, hook: "Wait for it", why: "Hooks win." }),
        { ...idea("v", "Verdict 1: pass", {}), type: "verdict" },
      ]);
    },
    "GET /api/runs/run_2/artifacts": () =>
      Response.json([idea("hooks", "Hook first", { rank: 1 }), { ...idea("d", "Decision at pick", {}), type: "decision" }, short]),
    "POST /api/runs/run_1/decision": decision("run_1"),
    "POST /api/runs/run_2/decision": decision("run_2"),
  });
  const { screen } = await mountAt("/gates");

  const pick = screen.getByTestId("gate-run_1");
  const cards = pick.getByTestId("pick-card");
  await expect.element(cards.first()).toMatchTextContent(/Erlich's #1 of 2.*Hook first.*Wait for it.*Why: Hooks win\./);
  await expect.element(cards.nth(1)).toMatchTextContent(/#2 of 2.*The chase/);
  await expect.element(pick.getByLabelText("Your pick")).not.toBeInTheDocument();
  expect(queries).toEqual(["?before=1"]);
  await pick.getByRole("radio", { name: /Hook first/ }).click();
  await pick.getByLabelText("Note (optional)").fill("Less text on screen.");
  await pick.getByRole("button", { name: "Continue with this pick" }).click();
  await vi.waitFor(() =>
    expect(bodies.run_1).toEqual({
      choice: "Hook first",
      artifact: "runs/run_1/stage_0/artifacts/idea-hooks.md",
      note: "Less text on screen.",
    }),
  );

  const approve = screen.getByTestId("gate-run_2");
  const review = approve.getByRole("region", { name: "Under review" });
  await expect.element(review.getByTestId("player")).toHaveAttribute(
    "src",
    "/api/worlds/world_1/files/runs/run_2/stage_2/artifacts/short.mp4",
  );
  await expect.element(review).not.toMatchTextContent(/Hook first/);
  await approve.getByRole("button", { name: "Approve" }).click();
  await vi.waitFor(() =>
    expect(bodies.run_2).toEqual({ note: "", artifact: "runs/run_2/stage_2/artifacts/short.mp4.md" }),
  );
});
