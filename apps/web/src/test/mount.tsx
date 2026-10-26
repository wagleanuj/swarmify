import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render } from "vitest-browser-react";

import { createAppRouter } from "../router";

/** Mount the whole app at `path` with an in-memory history. */
export async function mountAt(path: string) {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }));
  const screen = await render(<RouterProvider router={router} />);
  return { router, screen };
}
