import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import { FakeEventSource, fakeFetch, gate, run, stage } from "../test/fakes";
import { answerConfirm, mountAt } from "../test/mount";

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
    "GET /api/gates": () => Response.json(inbox, { headers: { "X-Event-Seq": "9" } }),
    "GET /api/worlds/world_1/runs": () =>
      Response.json([run("running", [], { id: "run_7", name: "Nightly short" }), run("done", [], { id: "run_8", name: "Old one" })]),
    "POST /api/runs/run_1/decision": decision("run_1"),
    "POST /api/runs/run_2/decision": decision("run_2"),
    "POST /api/runs/run_3/decision": decision("run_3"),
  });
  const { router, screen } = await mountAt("/gates");
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Needs you");
  await expect.element(screen.getByText(/3 decisions waiting, oldest .* ago\. Answering one opens the next\./)).toBeVisible();
  // One stream for every world: a stream per world exhausts the browser's six connections. It
  // starts where the gates' snapshot stands, so no run.updated in between is missed.
  expect(FakeEventSource.open.map((s) => s.url)).toEqual(["/api/events?since=9"]);
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

test("a world whose memory keeps failing to rebuild needs you too, until it rebuilds", async () => {
  const music = { ...video, id: "world_2", name: "Music" };
  const stalled = (world_id: string, world_name: string) => ({
    world_id,
    world_name,
    failures: 95,
    since: "2026-11-05T05:29:00+00:00",
    at: ts,
    error: "graphify extract exited 1: " + "x".repeat(400),
  });
  fakeFetch({
    "GET /api/worlds": () => Response.json([video, music]),
    "GET /api/gates": () => Response.json([]),
    "GET /api/graph/stalled": () => Response.json([stalled("world_1", "Video"), stalled("world_2", "Music")]),
  });
  const { screen } = await mountAt("/gates?world=world_1");
  const notice = screen.getByTestId("stalled-memory");
  await expect.element(notice).toMatchTextContent(/Video's memory has not updated since .* ago: its last 95 rebuilds failed/);
  await expect.element(notice).toMatchTextContent(/Agents may pitch what it already made\./);
  await expect.element(notice.getByRole("link", { name: "Open its memory" })).toHaveAttribute("href", "/worlds/world_1/graph");
  expect(notice.element().textContent!.length).toBeLessThan(500); // a long error is cut short
  expect(screen.getByText(/Music's memory/).elements()).toHaveLength(0); // another world's, filtered out
});

test("a refused sign-in, unread retention and a post still private a day on need you too", async () => {
  const music = { ...video, id: "world_2", name: "Music" };
  fakeFetch({
    "GET /api/worlds": () => Response.json([video, music]),
    "GET /api/gates": () => Response.json([]),
    "GET /api/notices": () =>
      Response.json([
        { kind: "sign_in", text: "YouTube needs sign-in: the provider refused its saved sign-in.", world_id: "world_2", run_id: null },
        { kind: "outcomes", text: "YouTube retention is not read: sign in again.", world_id: "world_2", run_id: null },
        { kind: "visibility", text: "Five hooks is still private on youtube a day on.", world_id: "world_1", run_id: "run_1" },
        { kind: "visibility", text: "Music video is still private on youtube a day on.", world_id: "world_2", run_id: "run_2" },
      ]),
  });
  const { screen } = await mountAt("/gates?world=world_1");
  const notices = screen.getByTestId("notice");
  await expect.element(notices.nth(0)).toMatchTextContent(/YouTube needs sign-in/);
  // a shared account shows in every world's view, a post only in its own
  expect(notices.elements()).toHaveLength(3);
  await expect.element(notices.nth(0).getByRole("link", { name: "Open Connectors" })).toHaveAttribute("href", "/worlds/world_2/connectors");
  await expect.element(notices.nth(1)).toMatchTextContent(/retention is not read/);
  await expect.element(notices.nth(2).getByRole("link", { name: "Open the run" })).toHaveAttribute("href", "/worlds/world_1/runs/run_1");
  expect(screen.getByText(/Music video/).elements()).toHaveLength(0);
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
    private: false,
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
          publish: { connector: "upload_post", fields: [...preview.fields.slice(0, 2), field("platforms", "Platforms", ["tiktok"]), ...later], private: false, problems: ["upload_post needs params.user, the Upload-Post profile to post as"] },
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
  const publish = { connector: "youtube", fields: [field("hashtags", tags, true), field("visibility", "private", false)], private: false, problems: ["7 hashtags, at most 5"] };
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
            private: false,
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
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_4");
  await expect.element(card.getByTestId("skill-change")).toBeVisible();
  await card.getByRole("button", { name: "Reject" }).click();
  expect(await answerConfirm(screen, "Cancel")).toContain("Reject without a reason?");
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

test("asking at a publish gate answers there, fills the form, and an applied file change refreshes what goes out, keeping the fills it left alone", async () => {
  const field = (key: string, label: string, value: unknown, editable: boolean) => ({ key, label, value, editable, hint: "" });
  const seven = "#a #b #c #d #e #f #g";
  const five = "#a #b #c #d #e";
  const preview = (title: string, tags: string, problems: string[]) => ({
    connector: "youtube",
    fields: [field("title", "Title", title, true), field("hashtags", "Hashtags", tags, true), field("visibility", "Visibility", "public", false)],
    private: false,
    problems,
  });
  let publish = preview("Owls", seven, ["7 hashtags, at most 5"]);
  const atGate = run("waiting_gate", [stage(0, "write", "connector", "done"), stage(1, "confirm", "gate", "waiting"), stage(2, "publish", "connector", "pending")], { revision: 3 });
  const meta = (tags: string) => `---\nhashtags: ${tags}\n---\n`;
  const change = { kind: "artifact_edit", stage: "stage_0", payload: { path: "metadata.md", text: meta(five) } };
  const proposal = { change, stage_name: "write", before: { path: "metadata.md", text: meta(seven) }, after: change.payload, violations: [], revision: 3, in_place: true, read_by: [] };
  const reply = { id: "message_2", role: "assistant", text: "Cut to five, here and in metadata.md.", at: ts, sources: [], cost_usd: 0.02, fields: { title: "Owls, in five tags", hashtags: five } };
  const asked: unknown[] = [];
  const applied: unknown[] = [];
  const marked: unknown[] = [];
  const calls = fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json([gate("confirm", { publish })]),
    "GET /api/runs/run_1": () => Response.json(atGate),
    "GET /api/runs/run_1/assistant": () => Response.json({ messages: [], spent_usd: 0, average_usd: 0.19, month_left_usd: 41.2 }),
    "POST /api/runs/run_1/assistant": async (req) => {
      asked.push(await req.json());
      return Response.json({ ...reply, proposals: [{ id: "proposal_1", state: "open", proposal, change_id: null }] });
    },
    "POST /api/runs/run_1/changes": async (req) => {
      applied.push(await req.json());
      publish = preview("Owls", five, []); // what metadata.md now says; the title stays
      const done = { id: "change_1", run_id: "run_1", kind: "artifact_edit", stage_id: "stage_0", stage_index: 0, stage_name: "write" };
      return Response.json({ ...done, payload: change.payload, before: proposal.before, by: "operator", at: ts, revision: 4 }, { status: 201 });
    },
    "POST /api/runs/run_1/assistant/proposals/proposal_1": async (req) => {
      marked.push(await req.json());
      return Response.json({});
    },
  });
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_1");
  const confirm = card.getByRole("button", { name: "Confirm and post" });
  await expect.element(confirm).toBeDisabled();
  await expect.element(card).toMatchTextContent(/Asking costs about \$0\.19 a question, from the \$41\.20 the world has left this month\./);

  await card.getByLabelText("Ask or change before you answer").fill("Cut the hashtags to five");
  await card.getByRole("button", { name: "Send" }).click();
  await expect.element(card).toMatchTextContent(/Cut to five, here and in metadata\.md\./);
  expect(asked).toEqual([{ message: "Cut the hashtags to five" }]);
  // The gate field fills the form instead of a card, and Confirm unlocks.
  await expect.element(card.getByLabelText("Hashtags")).toHaveValue(five);
  await expect.element(card).toMatchTextContent(/Filled Title, Hashtags in the form above\./);
  await expect.element(confirm).toBeEnabled();

  // The file edit is a card; applying it refreshes the preview from the server.
  const edit = card.getByTestId("change-card");
  await expect.element(edit.getByRole("heading", { name: "Edit metadata.md" })).toBeVisible();
  await expect.element(edit.getByRole("list", { name: "The change" })).toHaveTextContent(`hashtags: ${seven}hashtags: ${five}`);
  await edit.getByRole("button", { name: "Apply" }).click();
  await expect.element(edit).toMatchTextContent(/Applied by you at \d\d:\d\d/);
  expect(applied).toEqual([{ ...change, revision: 3 }]);
  await vi.waitFor(() => expect(marked).toEqual([{ state: "applied", change_id: "change_1" }]));
  await vi.waitFor(() => expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(2));
  await expect.element(card.getByText("7 hashtags, at most 5")).not.toBeInTheDocument();
  // The filled title outlives the refresh, since the change left the post's title alone.
  await expect.element(card.getByLabelText("Title")).toHaveValue("Owls, in five tags");
  await expect.element(card.getByLabelText("Hashtags")).toHaveValue(five);
  await expect.element(confirm).toBeEnabled();
});

test("asking at a gate offers a Rerun, a Skip and a Restructure as Apply cards there", async () => {
  const atGate = run("waiting_gate", [stage(0, "script", "swarm", "done"), stage(1, "pick", "gate", "waiting"), stage(2, "upload", "connector", "pending", { config: { calls: "youtube" } })], { revision: 3 });
  const review = { name: "review", type: "gate", ask: "Ready to upload?" };
  const upload = { name: "upload", type: "connector", calls: "youtube" };
  const proposal = (kind: string, stage: string, stage_name: string, payload: object, before: object, after: object) => ({
    change: { kind, stage, payload },
    stage_name,
    before,
    after,
    violations: [],
    revision: 3,
    in_place: kind !== "restructure",
    fork_from: kind === "restructure" ? "upload" : null,
  });
  const proposals = [
    proposal("rerun", "stage_0", "script", { note: "Stronger hook." }, { script: "done", pick: "waiting" }, { script: "pending", pick: "pending" }),
    proposal("skip", "stage_2", "upload", {}, { upload: "pending" }, { upload: "skipped" }),
    proposal("restructure", "stage_2", "upload", { stages: [review, upload] }, { stages: [upload] }, { stages: [review, upload] }),
  ].map((p, n) => ({ id: `proposal_${n}`, state: "open", proposal: p, change_id: null }));
  const applied: { kind: string }[] = [];
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json([gate("pick")]),
    "GET /api/runs/run_1": () => Response.json(atGate),
    "POST /api/runs/run_1/assistant": () => Response.json({ id: "message_2", role: "assistant", text: "Three ways.", at: ts, sources: [], cost_usd: 0.02, proposals }),
    "POST /api/runs/run_1/changes": async (req) => {
      const body = await req.json();
      applied.push(body);
      return Response.json({ id: "change_1", run_id: "run_1", kind: body.kind, stage_id: body.stage, stage_index: 0, stage_name: "script", payload: {}, before: {}, by: "operator", at: ts, revision: 4 }, { status: 201 });
    },
    ...Object.fromEntries(proposals.map((p) => [`POST /api/runs/run_1/assistant/proposals/${p.id}`, () => Response.json({})])),
  });
  const { screen } = await mountAt("/gates");
  const card = screen.getByTestId("gate-run_1");
  await card.getByLabelText("Ask or change before you answer").fill("Redo the script with a stronger hook");
  await card.getByRole("button", { name: "Send" }).click();

  const rerun = card.getByRole("region", { name: "Re-run from script" });
  await expect.element(rerun.getByRole("list", { name: "The change" })).toHaveTextContent("script done pendingpick waiting pending");
  await expect.element(card.getByRole("region", { name: "Skip upload" })).toBeVisible();
  const restructure = card.getByRole("region", { name: "Change the stages from upload on" });
  await expect.element(restructure.getByRole("list", { name: "The change" })).toMatchTextContent(/review \(gate\)/);
  for (const name of ["Re-run from script", "Skip upload", "Change the stages from upload on"]) {
    const change = card.getByRole("region", { name });
    await change.getByRole("button", { name: "Apply" }).click();
    await expect.element(change).toMatchTextContent(/Applied by you at \d\d:\d\d/);
  }
  expect(applied.map((a) => a.kind)).toEqual(["rerun", "skip", "restructure"]);
  expect(applied[0]).toEqual({ kind: "rerun", stage: "stage_0", payload: { note: "Stronger hook." }, revision: 3 });
});

test("the run page's own gate has no second assistant: Ask this run covers it", async () => {
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/runs/run_1": () => Response.json(run("waiting_gate", [stage(0, "pick", "gate", "waiting")])),
    "GET /api/gates": () => Response.json([gate("pick", { stage_id: "stage_0", stage_index: 0 })]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_0");
  await expect.element(screen.getByRole("form", { name: "Answer pick" })).toBeVisible();
  expect(screen.getByLabelText("Ask or change before you answer").elements()).toHaveLength(0);
});

test("on a phone, while the gates load, the card is a skeleton at its size, not a Loading line", async () => {
  await page.viewport(390, 844);
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => new Promise<Response>(() => undefined),
  });
  const { screen } = await mountAt("/gates");
  await expect.element(screen.getByRole("heading", { name: "Needs you", level: 1 })).toBeVisible();
  const busy = screen.container.querySelector("[aria-busy='true']");
  expect(busy).not.toBeNull();
  // the ask, two paragraph lines and the evidence block
  expect(busy!.querySelectorAll(".g-skeleton > .sk, .g-skeleton .sk-lines > .sk")).toHaveLength(4);
  expect(screen.container.textContent).not.toMatch(/Loading\.\.\./);
  await page.viewport(1280, 800);
});

/** Answer the first of two confirm gates and return the queue's next card. */
async function answerFirst() {
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json([inbox[0]!, gate("confirm", { run_id: "run_4", stage_id: "s4", run_name: "Second one" })]),
    "POST /api/runs/run_3/decision": () => Response.json(run("running", [], { id: "run_3" })),
  });
  const { screen } = await mountAt("/gates");
  await screen.getByTestId("gate-run_3").getByRole("button", { name: "Confirm" }).click();
  return screen;
}

test("after answering on a phone, the next card takes its place under the line saying what was decided", async () => {
  await page.viewport(390, 844);
  const screen = await answerFirst();
  await expect.element(screen.getByTestId("gate-run_4")).toBeVisible();
  await expect.element(screen.getByTestId("answered")).toMatchTextContent(/You confirmed it at .*\./);
  await expect.element(screen.getByTestId("gate-run_3")).not.toBeInTheDocument();
  // Reduced motion (as the tests run): an instant swap, no sliding copy of the answered card.
  expect(screen.container.querySelector(".gate-ghost")!.childElementCount).toBe(0);
  await page.viewport(1280, 800);
});

test("with motion on, the answered card slides out as a still copy that is gone once it has", async () => {
  const real = window.matchMedia.bind(window);
  vi.spyOn(window, "matchMedia").mockImplementation((q) =>
    q.includes("prefers-reduced-motion") ? ({ ...real(q), matches: false } as MediaQueryList) : real(q),
  );
  const copies: string[] = [];
  const watch = new MutationObserver((changes) =>
    changes
      .filter((c) => (c.target as HTMLElement).classList.contains("gate-ghost"))
      .forEach((c) => c.addedNodes.forEach((n) => copies.push(n.textContent ?? ""))),
  );
  watch.observe(document.body, { childList: true, subtree: true });
  const screen = await answerFirst();
  await expect.element(screen.getByTestId("gate-run_4")).toBeVisible();
  await vi.waitFor(() => expect(copies.join()).toMatch(/Publish it/));
  // the copy is inert and hidden from screen readers, and leaves when its slide ends
  await vi.waitFor(() => expect(screen.container.querySelector(".gate-ghost")!.childElementCount).toBe(0));
  expect(screen.container.querySelector(".gate-ghost")!.getAttribute("aria-hidden")).toBe("true");
  watch.disconnect();
});
