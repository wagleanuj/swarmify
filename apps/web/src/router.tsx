import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  type RouterHistory,
} from "@tanstack/react-router";

import { Runs } from "./pages/Runs";
import { RunTimeline } from "./pages/RunTimeline";
import { WorkflowEditor } from "./pages/WorkflowEditor";
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

export const newWorkflowRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/worlds/$worldId/workflows/new",
  component: WorkflowEditor,
});

export const workflowRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/worlds/$worldId/workflows/$workflowId",
  component: WorkflowEditor,
});

export const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/worlds/$worldId/runs",
  component: Runs,
});

export const runRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/worlds/$worldId/runs/$runId",
  component: RunTimeline,
});

const routeTree = rootRoute.addChildren([
  worldsRoute,
  worldRoute,
  newWorkflowRoute,
  workflowRoute,
  runsRoute,
  runRoute,
]);

// `history` is injectable so tests can mount a page at a given URL with a memory history.
export function createAppRouter(history?: RouterHistory) {
  return createRouter({ routeTree, history });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
