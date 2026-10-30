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
