import {
  createRootRoute,
  createRoute,
  createRouter,
  type RouterHistory,
} from "@tanstack/react-router";

import { Cast } from "./pages/Cast";
import { Connectors } from "./pages/Connectors";
import { Evals } from "./pages/Evals";
import { Graph } from "./pages/Graph";
import { Gates } from "./pages/Gates";
import { Library } from "./pages/Library";
import { Runs } from "./pages/Runs";
import { RunTimeline } from "./pages/RunTimeline";
import { Scorecard } from "./pages/Scorecard";
import { Settings } from "./pages/Settings";
import { WorkflowEditor } from "./pages/WorkflowEditor";
import { WorldOverview } from "./pages/WorldOverview";
import { Worlds } from "./pages/Worlds";
import { Shell } from "./Shell";

const rootRoute = createRootRoute({ component: Shell });

export const worldsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: Worlds,
});

export const gatesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/gates",
  component: Gates,
  // The open gate, the one just answered and the world filter live in the address.
  validateSearch: (search: Record<string, unknown>): { gate?: string; done?: string; world?: string } =>
    Object.fromEntries(
      (["gate", "done", "world"] as const).flatMap((k) => (typeof search[k] === "string" ? [[k, search[k]]] : [])),
    ),
});

export const evalsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/evals",
  component: Evals,
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

export const scorecardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/worlds/$worldId/workflows/$workflowId/scorecard",
  component: Scorecard,
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
  // `?stage=<id>` keeps the selected step, so a link opens the same one.
  validateSearch: (search: Record<string, unknown>): { stage?: string } =>
    typeof search.stage === "string" ? { stage: search.stage } : {},
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
  evalsRoute,
  worldRoute,
  newWorkflowRoute,
  workflowRoute,
  scorecardRoute,
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
