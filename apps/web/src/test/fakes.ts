/** Test doubles for the browser APIs the pages use: fetch routing and EventSource. */
import { vi } from "vitest";

import type { Run } from "../api/runs";

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
export const stage = (index: number, name: string, type: string, status: string) => ({
  id: `stage_${index}`,
  index,
  name,
  type,
  status,
  started_at: null,
  ended_at: null,
  error: null as string | null,
});

/** A run snapshot as the API and `run.updated` carry it. */
export function run(status: Run["status"], stages: ReturnType<typeof stage>[], over = {}): Run {
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
    ...over,
  } as Run;
}
