import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  type RouterHistory,
} from "@tanstack/react-router";

import { Cast } from "./pages/Cast";
import { Connectors } from "./pages/Connectors";
import { Graph } from "./pages/Graph";
import { Gates } from "./pages/Gates";
import { Library } from "./pages/Library";
import { Runs } from "./pages/Runs";
import { RunTimeline } from "./pages/RunTimeline";
import { Settings } from "./pages/Settings";
import { WorkflowEditor } from "./pages/WorkflowEditor";
import { WorldOverview } from "./pages/WorldOverview";
import { Worlds } from "./pages/Worlds";

const rootRoute = createRootRoute({ component: () => <Outlet /> });

export const worldsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: Worlds,
});

export const gatesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/gates",
  component: Gates,
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

export const connectorsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/worlds/$worldId/connectors",
  component: Connectors,
});

export const graphRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/worlds/$worldId/graph",
  component: Graph,
});

export const libraryRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/worlds/$worldId/library",
  component: Library,
});

export const castRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/worlds/$worldId/cast",
  component: Cast,
});

export const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/worlds/$worldId/settings",
  component: Settings,
});

const routeTree = rootRoute.addChildren([
  worldsRoute,
  gatesRoute,
  worldRoute,
  newWorkflowRoute,
  workflowRoute,
  runsRoute,
  runRoute,
  connectorsRoute,
  graphRoute,
  libraryRoute,
  castRoute,
  settingsRoute,
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
