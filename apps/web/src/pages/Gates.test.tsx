import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, gate, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:00:00+00:00";
const video = { id: "world_1", name: "Video", purpose: "", created_at: ts };
const inbox = [
  gate("confirm", { run_id: "run_3", stage_id: "s3", run_name: "Publish it" }),
  gate("approve", { run_id: "run_2", stage_id: "s2", send_backs_left: 1, max_send_backs: 5, earlier_stages: ["research", "script"] }),
  gate("pick", { run_id: "run_1", stage_id: "s1" }),
];

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("the queue opens the next decision after each answer and says what was decided", async () => {
  const bodies: Record<string, unknown> = {};
  const decision = (id: string) => async (req: Request) => {
    bodies[id] = await req.json();
    return Response.json(run("running", [stage(0, "x", "connector", "running")], { id }));
  };
  const calls = fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json(inbox),
    "GET /api/worlds/world_1/runs": () =>
      Response.json([run("running", [], { id: "run_7", name: "Nightly short" }), run("done", [], { id: "run_8", name: "Old one" })]),
    "POST /api/runs/run_1/decision": decision("run_1"),
    "POST /api/runs/run_2/decision": decision("run_2"),
    "POST /api/runs/run_3/decision": decision("run_3"),
  });
  const { router, screen } = await mountAt("/gates");
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Needs you");
  await expect.element(screen.getByText(/3 decisions waiting, oldest .* ago\. Answering one opens the next\./)).toBeVisible();
  // One stream for every world: a stream per world exhausts the browser's six connections.
  expect(FakeEventSource.open.map((s) => s.url)).toEqual(["/api/events?since=now"]);
  const queue = screen.getByRole("navigation", { name: "Waiting decisions" });
  await expect
    .element(queue)
    .toMatchTextContent(/Video.*ago.*Confirm before it goes out.*Publish it.*Review and approve.*Weekly short.*Pick an idea.*Weekly short/);
  // The open decision is the first, and carries the selection line.
  await expect.element(queue.getByRole("link", { name: /Publish it/ })).toHaveAttribute("aria-current");
  await expect.element(screen.getByTestId("gate-run_2")).not.toBeInTheDocument();

  const confirm = screen.getByTestId("gate-run_3");
  await expect.element(confirm).toMatchTextContent(/Nothing goes out until you confirm\./);
  await confirm.getByRole("button", { name: "Confirm" }).click();
  await vi.waitFor(() => expect(bodies.run_3).toEqual({ note: "" }));
  // The next one opens, a line says what was just decided, and both live in the address.
  const approve = screen.getByTestId("gate-run_2");
  await expect.element(approve).toBeVisible();
  await expect.element(screen.getByTestId("answered")).toMatchTextContent(/You confirmed it at .*\./);
  await expect
    .element(screen.getByTestId("answered").getByRole("link", { name: "Open that run" }))
    .toHaveAttribute("href", "/worlds/world_1/runs/run_3?stage=s3");
  expect(router.state.location.search).toEqual({ gate: "s2", done: "s3" });
  // Answered items sort last, dimmed.
  await expect.element(queue).toMatchTextContent(/Review and approve.*Pick an idea.*Answered.*Publish it/);
  await expect.element(queue.getByRole("link", { name: /Publish it/ })).toHaveClass("done");
  await expect.element(queue.getByRole("link", { name: /Review and approve/ })).toHaveAttribute("aria-current");

  await expect.element(approve).toMatchTextContent(/1 of 5 revisions left/);
  await approve.getByLabelText(/^Note/).fill("Too much text.");
  await approve.getByRole("button", { name: "Send back to..." }).click();
  // The nearest earlier step first, each saying what sending back there does.
  await expect.element(approve.getByRole("menu")).toMatchTextContent(/^script\s*Redo script with your notes\s*research\s*Redo research with your notes$/);
  await approve.getByRole("menuitem", { name: /^research/ }).click();
  await vi.waitFor(() =>
    expect(bodies.run_2).toEqual({ note: "Too much text.", send_back_to: "research" }),
  );
  await expect.element(screen.getByTestId("answered")).toMatchTextContent(/You sent it back to research at/);

  const pick = screen.getByTestId("gate-run_1");
  await expect.element(pick).toMatchTextContent(/Pick one idea\./);
  await pick.getByLabelText("Your pick").fill("idea 2");
  await pick.getByLabelText("Note (optional)").fill("Less text on screen.");
  await pick.getByRole("button", { name: "Continue with this pick" }).click();
  await vi.waitFor(() => expect(bodies.run_1).toEqual({ choice: "idea 2", note: "Less text on screen." }));
  await expect.element(screen.getByTestId("answered")).toMatchTextContent(/You picked "idea 2" at/);

  // An empty queue says so and lists the runs still moving.
  const empty = screen.getByTestId("no-gates");
  await expect.element(empty).toMatchTextContent(/Nothing is waiting on you/);
  await expect.element(empty.getByRole("link", { name: "Nightly short" })).toHaveAttribute("href", "/worlds/world_1/runs/run_7");
  await expect.element(empty.getByRole("link", { name: "Old one" })).not.toBeInTheDocument();
  // Each answer hides its card at once, without refetching before the run moves.
  expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(1);
});

test("a world filter scopes the queue, and a queue item opens its gate", async () => {
  const music = { ...video, id: "world_2", name: "Music" };
  fakeFetch({
    "GET /api/worlds": () => Response.json([video, music]),
    "GET /api/gates": () => Response.json([inbox[0]!, { ...inbox[1]!, world_id: "world_2", run_name: "Album teaser" }]),
  });
  const { router, screen } = await mountAt("/gates");
  const queue = screen.getByRole("navigation", { name: "Waiting decisions" });
  await expect.element(queue).toMatchTextContent(/Publish it.*Music.*Album teaser/);
  await queue.getByRole("link", { name: /Album teaser/ }).click();
  await expect.element(screen.getByTestId("gate-run_2")).toBeVisible();
  expect(router.state.location.search).toEqual({ gate: "s2" });

  await screen.getByRole("combobox", { name: /^World/ }).selectOptions("Video");
  await expect.element(queue).not.toMatchTextContent(/Album teaser/);
  await expect.element(screen.getByTestId("gate-run_3")).toBeVisible();
  expect(router.state.location.search).toEqual({ world: "world_1" });
  await expect.element(screen.getByText(/^One decision waiting/)).toBeVisible();
});

test("the world overview's Answer opens the gate itself", async () => {
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/gates": () => Response.json([inbox[1]!, { ...inbox[0]!, world_id: "world_2" }]),
  });
  const { router, screen } = await mountAt("/worlds/world_1");
  await screen.getByTestId("needs-you").getByRole("link", { name: "Answer" }).click();
  await expect.element(screen.getByTestId("gate-run_2")).toBeVisible();
  expect(router.state.location.search).toEqual({ world: "world_1", gate: "s2" });
});

test("approve without revisions left says so, and a refused decision is an alert", async () => {
  const calls = fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json([gate("approve", { send_backs_left: 0 })]),
    "POST /api/runs/run_1/decision": () =>
      Response.json({ detail: "already sent back 2 times; approve or abort" }, { status: 409 }),
  });
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_1");
  await expect.element(card).toMatchTextContent(/No revisions left at this gate/);
  await expect.element(card.getByRole("button", { name: "Send back to..." })).not.toBeInTheDocument();
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
  await expect.element(screen.getByTestId("queue")).toMatchTextContent(/Publish it/);
  open = [];
  FakeEventSource.emit("run.updated", { payload: run("done", []) });
  await expect.element(screen.getByTestId("no-gates")).toMatchTextContent(/Nothing is waiting on you.*No runs are moving\./);
  expect(calls.filter((c) => c === "GET /api/gates").length).toBeGreaterThanOrEqual(2);
});

test("a gate before a publish stage shows exactly what goes out, and refuses when it cannot", async () => {
  const field = (key: string, label: string, value: unknown, editable = false, hint = "") => ({ key, label, value, editable, hint });
  const preview = {
    connector: "youtube",
    fields: [
      field("title", "Title", "Five hooks that work", true),
      field("description", "Description", "Hooks, ranked.", true),
      field("visibility", "Visibility", "public"),
      field("made_for_kids", "Made for kids", true),
      field("schedule", "When", "2099-01-01T09:00:00Z", false, "Private until then, then public."),
      field("video", "Video", "short.mp4"),
    ],
    problems: [],
  };
  const later = preview.fields.slice(2).map((f) => (f.key === "schedule" ? { ...f, value: null, hint: "Right after confirming." } : f));
  const bodies: unknown[] = [];
  const calls = fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () =>
      Response.json([
        gate("confirm", { publish: preview }),
        gate("confirm", {
          run_id: "run_2",
          stage_id: "s2",
          publish: { connector: "upload_post", fields: [...preview.fields.slice(0, 2), field("platforms", "Platforms", ["tiktok"]), ...later], problems: ["upload_post needs params.user, the Upload-Post profile to post as"] },
        }),
        gate("approve", {
          run_id: "run_3",
          stage_id: "s3",
          publish: { ...preview, problems: ["a publish stage needs params.title"] },
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
  await expect.element(out.getByLabelText("Title")).toHaveValue("Five hooks that work");
  await expect.element(out.getByLabelText("Description")).toHaveValue("Hooks, ranked.");
  await expect.element(out).toMatchTextContent(/Made for kids\s*Yes/);
  await expect.element(out).toMatchTextContent(/Where\s*YouTube/);
  await expect.element(out).toMatchTextContent(/Visibility\s*public/);
  await expect.element(out).toMatchTextContent(/2099-01-01 09:00 UTC\)\.\s*Private until then, then public\./);
  await expect.element(out).toMatchTextContent(/Video\s*short\.mp4/);
  await expect.element(out.getByRole("time")).toHaveAttribute("datetime", "2099-01-01T09:00:00Z");
  await expect.element(card).toMatchTextContent(/Posts exactly what is above\./);
  await out.getByLabelText("Title").fill("Five better hooks");
  await out.getByLabelText("Description").fill("Edited.");
  await card.getByRole("button", { name: "Confirm and post" }).click();
  await vi.waitFor(() =>
    expect(bodies).toEqual([
      {
        note: "",
        edits: { title: "Five better hooks", description: "Edited." },
        seen: { visibility: "public", made_for_kids: true, schedule: "2099-01-01T09:00:00Z", video: "short.mp4" },
      },
    ]),
  );

  const queue = screen.getByRole("navigation", { name: "Waiting decisions" });
  await queue.getByRole("link", { name: /Weekly short/ }).first().click();
  const broken = screen.getByTestId("gate-run_2");
  await expect.element(broken).toMatchTextContent(/Where\s*Upload-Post\s*Title.*Platforms\s*tiktok/);
  await expect.element(broken).toMatchTextContent(/Right after confirming/);
  await expect.element(broken.getByRole("alert")).toMatchTextContent(/This will not post:.*needs params\.user/);
  await expect.element(broken.getByRole("button", { name: "Confirm and post" })).toBeDisabled();
  await queue.getByRole("link", { name: /Review and approve/ }).click();
  const approve = screen.getByTestId("gate-run_3");
  await expect.element(approve.getByRole("alert")).toMatchTextContent(/needs params\.title/);
  await expect.element(approve.getByRole("button", { name: "Approve" })).toBeDisabled();
  expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(1);
});

test("a post blocked by an editable field unlocks once that field is edited, and sends the edit", async () => {
  const field = (key: string, value: unknown, editable: boolean) => ({ key, label: key, value, editable, hint: "" });
  const tags = "#shorts #animals #pets #cats #cat #science #animalfacts";
  const publish = { connector: "youtube", fields: [field("hashtags", tags, true), field("visibility", "private", false)], problems: ["7 hashtags, at most 5"] };
  const bodies: unknown[] = [];
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json([gate("confirm", { publish })]),
    "POST /api/runs/run_1/decision": async (req) => {
      bodies.push(await req.json());
      return Response.json(run("running", []));
    },
  });
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_1");
  await expect.element(card.getByRole("alert")).toMatchTextContent(/7 hashtags, at most 5.*Edit the fields above/);
  await expect.element(card.getByRole("button", { name: "Confirm and post" })).toBeDisabled();
  await card.getByLabelText("hashtags").fill("#shorts #animals #pets #cats #science");
  await card.getByRole("button", { name: "Confirm and post" }).click();
  await vi.waitFor(() =>
    expect(bodies).toEqual([{ note: "", edits: { hashtags: "#shorts #animals #pets #cats #science" }, seen: { visibility: "private" } }]),
  );
});

test("a gate before a text post shows the connector's own fields, and sends the edited one", async () => {
  const bodies: unknown[] = [];
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () =>
      Response.json([
        gate("confirm", {
          publish: {
            connector: "webhook",
            fields: [
              { key: "to", label: "To", value: "#news", editable: false, hint: "" },
              { key: "text", label: "Message", value: "New video is up", editable: true, hint: "" },
            ],
            problems: [],
          },
        }),
      ]),
    "POST /api/runs/run_1/decision": async (req) => {
      bodies.push(await req.json());
      return Response.json(run("running", []));
    },
  });
  const { screen } = await mountAt("/gates");
  const out = screen.getByTestId("gate-run_1").getByRole("region", { name: "What goes out" });
  await expect.element(out).toMatchTextContent(/Where\s*webhook\s*To\s*#news\s*Message/);
  await out.getByLabelText("Message").fill("It is up");
  await screen.getByRole("button", { name: "Confirm and post" }).click();
  await vi.waitFor(() => expect(bodies).toEqual([{ note: "", edits: { text: "It is up" }, seen: { to: "#news" } }]));
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
  await expect.element(card.getByRole("button", { name: "Send back to..." })).not.toBeInTheDocument();
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
  const { screen } = await mountAt("/gates?gate=s1");

  const pick = screen.getByTestId("gate-run_1");
  const cards = pick.getByTestId("pick-card");
  // A ranked list: rank, title and hook, the judge's reasoning folded into a disclosure.
  await expect.element(cards.first()).toMatchTextContent(/The judge's #1 of 2.*Hook first.*Wait for it.*Why it ranked here/);
  await expect.element(cards.first().getByText("Why: Hooks win.")).not.toBeVisible();
  await cards.first().getByText("Why it ranked here").click();
  await expect.element(cards.first().getByText("Why: Hooks win.")).toBeVisible();
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

test("an approve gate shows a typed script's top-level text, as the voice its judge chose", async () => {
  const value = { voice: "narrator", description: "Why hedgehogs sleep.", scenes: [{ id: "s01", lines: [] }] };
  const script = {
    ...short,
    name: "script.md",
    path: "runs/run_2/stage_1/artifacts/script.md",
    type: "script",
    title: "Hedgehogs",
    fields: { voice: "narrator" },
    body: `# Hedgehogs\n\n\`\`\`json\n${JSON.stringify(value, null, 2)}\n\`\`\`\n\nWho sleeps all winter?`,
  };
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json([inbox[1]!]),
    "GET /api/runs/run_2/artifacts": () => Response.json([script]),
  });
  const { screen } = await mountAt("/gates");
  const typed = screen.getByTestId("gate-run_2").getByTestId("typed-text");
  await expect.element(typed).toMatchTextContent(/voice\s*narrator\s*description\s*Why hedgehogs sleep\./);
  await expect.element(typed).not.toMatchTextContent(/scenes/);
});
