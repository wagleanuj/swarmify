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
  // Each answer refetches the inbox; wait for the last one so it never outlives the fake fetch.
  await vi.waitFor(() => expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(4));
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
