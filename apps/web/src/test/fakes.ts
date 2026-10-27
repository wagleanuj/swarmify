/** Test doubles for the browser APIs the pages use: fetch routing and EventSource. */
import { vi } from "vitest";

import type { Gate, Run } from "../api/runs";
import type { Task } from "../api/tasks";

type Handler = (req: Request) => Response | Promise<Response>;

/** Route fetch calls by `METHOD /path`; unmatched calls return 404. Returns the call log. */
export function fakeFetch(routes: Record<string, Handler>) {
  const calls: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input, init);
    const key = `${req.method} ${new URL(req.url).pathname}`;
    calls.push(key);
    const handler = routes[key];
    return handler ? handler(req) : new Response("not found", { status: 404 });
  });
  return calls;
}

/** A controllable EventSource: `FakeEventSource.emit(type, data)` reaches every open source. */
export class FakeEventSource extends EventTarget {
  static open: FakeEventSource[] = [];
  readonly url: string;
  constructor(url: string) {
    super();
    this.url = url;
    FakeEventSource.open.push(this);
  }
  close() {
    FakeEventSource.open = FakeEventSource.open.filter((s) => s !== this);
  }
  static emit(type: string, data: object) {
    for (const s of FakeEventSource.open) {
      s.dispatchEvent(new MessageEvent(type, { data: JSON.stringify(data) }));
    }
  }
  static install() {
    FakeEventSource.open = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  }
}

const ts = "2026-10-26T10:00:00+00:00";
type StageView = Run["stages"][number];
export const stage = (
  index: number,
  name: string,
  type: string,
  status: string,
  over: Partial<StageView> = {},
): StageView => ({
  id: `stage_${index}`,
  index,
  name,
  type,
  status: status as StageView["status"],
  started_at: null,
  ended_at: null,
  error: null as string | null,
  notes: [] as string[],
  ...over,
});

/** A run snapshot as the API and `run.updated` carry it. */
export function run(status: Run["status"], stages: StageView[], over = {}): Run {
  return {
    id: "run_1",
    world_id: "world_1",
    workflow_id: "wf_1",
    workflow_version: 2,
    name: "Render then pick",
    status,
    created_at: ts,
    updated_at: ts,
    stages,
    send_backs: 0,
    parent_run_id: null,
    decisions: [],
    budget_usd: null,
    spent_usd: 0,
    over_budget: false,
    baseline: false,
    ...over,
  } as Run;
}

/** A task snapshot as the API and `task.updated` carry it. */
export function task(id: string, status: Task["status"], over: Partial<Task> = {}): Task {
  return {
    id,
    world_id: "world_1",
    run_id: "run_1",
    stage_id: "stage_0",
    title: `Task ${id}`,
    body: "",
    status,
    owner: status === "open" ? null : "gilfoyle",
    added_by: "richard",
    heartbeat_at: status === "claimed" ? ts : null,
    created_at: ts,
    updated_at: ts,
    depends_on: [],
    notes: [],
    ...over,
  };
}

/** An open gate as `GET /api/gates` lists it. */
export function gate(kind: "pick" | "approve" | "confirm" | "budget" | "skill", over: Partial<Gate> = {}): Gate {
  return {
    run_id: "run_1",
    world_id: "world_1",
    run_name: "Weekly short",
    stage_id: "stage_1",
    stage_index: 1,
    stage_name: kind,
    kind,
    ask: {
      pick: "Pick one idea.",
      approve: "Watch it. Approve or send back.",
      confirm: "Publish Tuesday 9am?",
      budget: "The budget of $5.00 is spent ($5.20 so far, an estimate). Add money to go on, or abort.",
      skill: "The retro read this run and proposes a change. Approve it, edit it, or reject it.",
    }[kind],
    opened_at: ts,
    send_backs_left: 2,
    earlier_stages: ["research"],
    parent_run_id: null,
    ...over,
  };
}
