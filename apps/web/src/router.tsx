import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  type RouterHistory,
} from "@tanstack/react-router";

import { WorldOverview } from "./pages/WorldOverview";
import { Worlds } from "./pages/Worlds";

const rootRoute = createRootRoute({ component: () => <Outlet /> });

export const worldsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: Worlds,
});

export const worldRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/worlds/$worldId",
  component: WorldOverview,
});

const routeTree = rootRoute.addChildren([worldsRoute, worldRoute]);

// `history` is injectable so tests can mount a page at a given URL with a memory history.
export function createAppRouter(history?: RouterHistory) {
  return createRouter({ routeTree, history });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
