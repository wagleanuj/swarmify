/** Test doubles for the browser APIs the pages use: fetch routing and EventSource. */
import { vi } from "vitest";

import type { Gate, Run } from "../api/runs";
import type { components } from "../api/schema";
import type { Task } from "../api/tasks";

type Handler = (req: Request) => Response | Promise<Response>;

type Connector = components["schemas"]["ConnectorView"];
const builtin = (name: string, label: string, kind: Connector["kind"], over: Partial<Connector> = {}): Connector => ({
  id: `builtin_${name}`,
  world_id: null,
  name,
  kind,
  command: ["python", "-m", name],
  url: null,
  credentials: [],
  stage_types: ["connector"],
  builtin: true,
  about: "",
  created_at: null,
  health: null,
  services: [],
  types: {},
  input_types: [],
  world_types: null,
  label,
  capabilities: [],
  outputs: [],
  missing: [],
  sign_in: [],
  version: null,
  ...over,
});
const video = [{ description: "the video", media_type: "video/mp4" }];
/** The built-ins as `GET /api/worlds/world_1/connectors` describes them, which every fakeFetch
 * answers unless a test routes it itself. */
export const CONNECTORS: Connector[] = [
  builtin("remotion_render", "Remotion render", "stage", { outputs: video }),
  builtin("remotion_build", "Remotion build", "stage", { command: [], capabilities: ["build_session"], outputs: video }),
  builtin("youtube", "YouTube", "publish"),
  // the default packages, given to every world
  builtin("upload_post", "Upload-Post", "publish", {
    id: "package_upload_post",
    world_id: "world_1",
    builtin: false,
    command: [".venv/bin/python", "upload_post.py"],
    version: "1.0.0",
    capabilities: ["publish"],
  }),
  builtin("speech", "Speech", "stage", {
    id: "package_speech",
    world_id: "world_1",
    builtin: false,
    command: [".venv/bin/python", "speech.py"],
    version: "1.0.0",
    services: ["engines"],
    outputs: [
      { description: "timings.json, with the takes in narration/ beside it", media_type: "application/json" },
      { description: "the takes, one per scene, in narration/", media_type: "audio/wav" },
    ],
  }),
];

/** Route fetch calls by `METHOD /path`; unmatched calls return 404. Returns the call log. */
export function fakeFetch(given: Record<string, Handler>) {
  const routes: Record<string, Handler> = { "GET /api/worlds/world_1/connectors": () => Response.json(CONNECTORS), ...given };
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
    max_send_backs: 2,
    earlier_stages: ["research"],
    parent_run_id: null,
    ...over,
  };
}
