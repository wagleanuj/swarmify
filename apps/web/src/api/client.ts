import createClient from "openapi-fetch";

import type { paths } from "./schema";

// Resolve fetch per call so tests can stub globalThis.fetch after this module loads.
export const api = createClient<paths>({ fetch: (req) => globalThis.fetch(req) });
