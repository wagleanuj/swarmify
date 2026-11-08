import createClient from "openapi-fetch";

import type { paths } from "./schema";

/** Dispatched on window when the API refuses a call for want of a session. */
export const SIGNED_OUT = "swarmify:signed-out";

// Resolve fetch per call so tests can stub globalThis.fetch after this module loads.
export const api = createClient<paths>({ fetch: (req) => globalThis.fetch(req) });

// A session that expires or is signed out elsewhere sends the app back to its sign-in page.
// The sign-in call's own 401 is a wrong password, not that.
api.use({
  onResponse({ request, response }) {
    if (response.status === 401 && new URL(request.url).pathname !== "/api/session") {
      window.dispatchEvent(new Event(SIGNED_OUT));
    }
  },
});

// Requests in flight through `api`, for the route progress line (Progress.tsx). The event
// stream is an EventSource, not a fetch, so it is never counted.
let inFlight = 0;
const watchers = new Set<() => void>();
const bump = (by: number) => {
  inFlight = Math.max(0, inFlight + by);
  watchers.forEach((w) => w());
};
export const requestsInFlight = () => inFlight;
export const watchRequests = (onChange: () => void) => {
  watchers.add(onChange);
  return () => void watchers.delete(onChange);
};
api.use({
  onRequest: () => bump(1),
  onResponse: () => bump(-1),
  onError: () => bump(-1),
});
