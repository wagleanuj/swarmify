import { Link, type LinkProps, Outlet, useLocation } from "@tanstack/react-router";
import { type ReactNode, useEffect, useState } from "react";

import { api } from "./api/client";
import { useOpenGates } from "./api/gates";
import type { components } from "./api/schema";
import { Health } from "./Health";

type World = components["schemas"]["World"];

const ICON: Record<string, ReactNode> = {
  gates: <path d="M3 12h4l2 3h6l2-3h4M5 5h14l2 7v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6z" />,
  overview: <path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z" />,
  runs: <path d="M5 4l14 8-14 8z" />,
  workflows: <path d="M4 6h6v4H4zM14 14h6v4h-6zM7 10v4a2 2 0 0 0 2 2h5" />,
  library: <path d="M4 4h5v16H4zM10 4h4v16h-4zM15 5l4-1 2 15-4 1z" />,
  graph: <path d="M8 7l8 1M7 8l2 8M17 10l-6 7M4 6a2 2 0 1 0 4 0a2 2 0 1 0-4 0M16 8a2 2 0 1 0 4 0a2 2 0 1 0-4 0M8 18a2 2 0 1 0 4 0a2 2 0 1 0-4 0" />,
  cast: <path d="M6 8a3 3 0 1 0 6 0a3 3 0 1 0-6 0M3 20c0-3 3-5 6-5s6 2 6 5M16 5a3 3 0 0 1 0 6M18 15c2 1 3 3 3 5" />,
  connectors: <path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4" />,
  settings: <path d="M9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" />,
  worlds: <path d="M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />,
  evals: <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  close: <path d="M6 6l12 12M18 6L6 18" />,
  chev: <path d="M8 10l4 4 4-4" />,
};

const Icon = ({ name }: { name: string }) => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {ICON[name]}
  </svg>
);

const Mark = () => (
  <span className="brand">
    <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 18 L12 4 L20 18 Z" fill="none" stroke="var(--accent)" strokeWidth="2.2" strokeLinejoin="round" />
      <path d="M9 18 L12 12.5 L15 18" fill="var(--accent)" />
    </svg>
    Swarmify
  </span>
);

/** The app shell: a labelled sidebar around every screen, folded to a top bar on a phone. */
export function Shell() {
  const { pathname, hash } = useLocation();
  // `/worlds/<id>/<area>`, or a global route: `/` (all worlds), `/gates`, `/evals`.
  const [, top, routeWorld, area] = pathname.split("/");
  const current = top === "worlds" ? (area ?? (hash === "workflows" ? "workflows" : "overview")) : top || "worlds";
  const inWorld = top === "worlds" ? routeWorld : undefined;

  const gates = useOpenGates().gates;
  const [worlds, setWorlds] = useState<World[]>([]);
  // Global routes keep the last world the person was in; before any, the first one.
  const [last, setLast] = useState<string | undefined>(inWorld);
  if (inWorld && inWorld !== last) setLast(inWorld);
  const worldId = inWorld ?? last ?? worlds[0]?.id;
  const [open, setOpen] = useState(false);

  // Refetch on every world change, so a world made a moment ago has its name.
  useEffect(() => {
    api
      .GET("/api/worlds")
      .then(({ data }) => data && setWorlds(data))
      .catch(() => undefined);
  }, [worldId]);
  useEffect(() => setOpen(false), [pathname, hash]);

  const item = (key: string, label: string, link: LinkProps, count?: number) => (
    <Link
      {...link}
      className="nav-item"
      // Every Link marks itself current on an exact match; `current` covers the rest.
      activeOptions={{ exact: true, includeHash: true }}
      aria-current={current === key ? "page" : undefined}
    >
      <Icon name={key} />
      <span className="nav-label">{label}</span>
      {count ? (
        <span className="nav-count" aria-label={`${count} waiting`}>
          {count}
        </span>
      ) : null}
    </Link>
  );
  const params = { worldId: worldId ?? "" };

  return (
    <div className="app">
      <header className="topbar">
        <Mark />
        <button type="button" className="icon" aria-label="Menu" aria-expanded={open} aria-controls="sidebar" onClick={() => setOpen(true)}>
          <Icon name="menu" />
        </button>
      </header>
      <nav id="sidebar" className={open ? "sidebar open" : "sidebar"} aria-label="Main">
        <div className="sidebar-head">
          <Mark />
          <button type="button" className="icon menu-close" aria-label="Close menu" onClick={() => setOpen(false)}>
            <Icon name="close" />
          </button>
        </div>
        <Link to="/" className="switcher" activeOptions={{ exact: true, includeHash: true }} data-testid="world-switcher">
          <span>
            <small>World</small>
            <b>{worlds.find((w) => w.id === worldId)?.name ?? (worldId ? "..." : "Choose a world")}</b>
          </span>
          <Icon name="chev" />
        </Link>
        <div className="nav-group">
          {item("gates", "Needs you", { to: "/gates" }, gates?.length)}
          {worldId && (
            <>
              {item("overview", "Overview", { to: "/worlds/$worldId", params })}
              {item("runs", "Runs", { to: "/worlds/$worldId/runs", params })}
              {item("workflows", "Workflows", { to: "/worlds/$worldId", params, hash: "workflows" })}
              {item("library", "Library", { to: "/worlds/$worldId/library", params })}
              {item("graph", "Memory", { to: "/worlds/$worldId/graph", params })}
            </>
          )}
        </div>
        {worldId && (
          <div className="nav-group">
            {item("cast", "Cast", { to: "/worlds/$worldId/cast", params })}
            {item("connectors", "Connectors", { to: "/worlds/$worldId/connectors", params })}
            {item("settings", "Settings", { to: "/worlds/$worldId/settings", params })}
          </div>
        )}
        <div className="nav-group nav-foot">
          {item("worlds", "All worlds", { to: "/" })}
          {item("evals", "Evals", { to: "/evals" })}
          <Health />
        </div>
      </nav>
      <div className="canvas">
        <Outlet />
      </div>
    </div>
  );
}
