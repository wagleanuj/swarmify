/** Test doubles for the browser APIs the pages use: fetch routing and EventSource. */
import { vi } from "vitest";

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
