import { Link, type LinkProps, Outlet, useLocation } from "@tanstack/react-router";
import { createContext, type ReactNode, useEffect, useState } from "react";

import { api } from "./api/client";
import { useOpenGates } from "./api/gates";
import { money } from "./api/runs";
import type { components } from "./api/schema";
import { Health } from "./Health";
import { status } from "./pages/Connectors";
import { figures } from "./pages/WorldOverview";
import { Sheet } from "./Sheet";

type World = components["schemas"]["World"];

const ICON: Record<string, ReactNode> = {
  gates: <path d="M3 12h4l2 3h6l2-3h4M5 5h14l2 7v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6z" />,
  overview: <path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z" />,
  runs: <path d="M5 4l14 8-14 8z" />,
  workflows: <path d="M4 6h6v4H4zM14 14h6v4h-6zM7 10v4a2 2 0 0 0 2 2h5" />,
  library: <path d="M4 4h5v16H4zM10 4h4v16h-4zM15 5l4-1 2 15-4 1z" />,
  assets: <path d="M9 17V5l11-2v12M4 17a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0M15 15a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0" />,
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

// The phone's tab bar draws its own 24px icons, filled when current.
const TAB_ICON: Record<string, string> = {
  gates: "M4 13l3-8h10l3 8v6H4zM4 13h5l1 2h4l1-2h5",
  runs: "M7 5l12 7-12 7z",
  workflows: "M3.5 6a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0M15.5 18a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0M8.5 6H14a4 4 0 0 1 4 4v5.5",
  library: "M5 4v16M10 4v16M15 5l4 14",
  more: "M3.6 12a1.4 1.4 0 1 0 2.8 0a1.4 1.4 0 1 0-2.8 0M10.6 12a1.4 1.4 0 1 0 2.8 0a1.4 1.4 0 1 0-2.8 0M17.6 12a1.4 1.4 0 1 0 2.8 0a1.4 1.4 0 1 0-2.8 0",
};
const TabIcon = ({ name }: { name: string }) => (
  <svg viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d={TAB_ICON[name]} />
  </svg>
);
// Areas reached through More, so its tab is current on them.
const MORE = new Set(["overview", "assets", "graph", "cast", "connectors", "settings", "worlds", "evals"]);
// Switching worlds keeps the area you are in; anywhere else lands on the world's overview.
const SAME_AREA = {
  runs: "/worlds/$worldId/runs",
  workflows: "/worlds/$worldId/workflows",
  library: "/worlds/$worldId/library",
  assets: "/worlds/$worldId/assets",
  graph: "/worlds/$worldId/graph",
  cast: "/worlds/$worldId/cast",
  connectors: "/worlds/$worldId/connectors",
  settings: "/worlds/$worldId/settings",
} as const;
const count = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

export const Mark = () => (
  <span className="brand">
    <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 18 L12 4 L20 18 Z" fill="none" stroke="var(--accent)" strokeWidth="2.2" strokeLinejoin="round" />
      <path d="M9 18 L12 12.5 L15 18" fill="var(--accent)" />
    </svg>
    Swarmify
  </span>
);

/** The world the switcher shows, for global screens that mark it. */
export const CurrentWorld = createContext<string | undefined>(undefined);

/** The app shell: a labelled sidebar around every screen, folded behind a menu on a narrow
 * screen; on a phone, a world chip on top and a bottom tab bar with More instead. */
export function Shell() {
  const { pathname, hash } = useLocation();
  // `/worlds/<id>/<area>`, or a global route: `/` (all worlds), `/gates`, `/evals`.
  const [, top, routeWorld, area] = pathname.split("/");
  const current = top === "worlds" ? (area ?? "overview") : top || "worlds";
  const inWorld = top === "worlds" ? routeWorld : undefined;

  const gates = useOpenGates().gates;
  const [worlds, setWorlds] = useState<World[]>([]);
  // Global routes keep the last world the person was in; before any, the first one.
  const [last, setLast] = useState<string | undefined>(inWorld);
  if (inWorld && inWorld !== last) setLast(inWorld);
  const worldId = inWorld ?? last ?? worlds[0]?.id;
  const [open, setOpen] = useState(false);
  const [sheet, setSheet] = useState<"world" | "more" | null>(null);
  const [summary, setSummary] = useState<Record<string, string>>({});

  // Refetch on every world change, so a world made a moment ago has its name.
  useEffect(() => {
    api
      .GET("/api/worlds")
      .then(({ data }) => data && setWorlds(data))
      .catch(() => undefined);
  }, [worldId]);
  useEffect(() => {
    setOpen(false);
    setSheet(null);
  }, [pathname, hash]);

  // More's one-line summaries, read each time it opens.
  useEffect(() => {
    if (sheet !== "more" || !worldId) return;
    setSummary({});
    const path = { params: { path: { world_id: worldId } } };
    void Promise.all([
      api.GET("/api/worlds/{world_id}/runs", path).then(({ data }) => {
        if (!data) return {};
        const { today, spent } = figures(data, []);
        return { overview: today ? `${count(today, "run")} today · ${money(spent)}` : "no runs today" };
      }),
      api.GET("/api/worlds/{world_id}/cast", path).then(({ data }) => (data ? { cast: count(data.length, "agent") } : {})),
      api
        .GET("/api/worlds/{world_id}/connectors", path)
        .then(({ data }) => (data ? { connectors: `${data.filter((c) => status(c).tone === "ok").length} of ${data.length} ready` } : {})),
      api.GET("/api/worlds/{world_id}/assets", path).then(({ data }) => (data ? { assets: count(data.length, "file") } : {})),
    ].map((p) => p.catch(() => ({})))).then((parts) => setSummary(Object.assign({}, ...parts)));
  }, [sheet, worldId]);

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
  const worldName = worlds.find((w) => w.id === worldId)?.name ?? (worldId ? "..." : "Choose a world");

  const tab = (key: string, label: string, link: LinkProps, badge?: number) => (
    <Link {...link} className={current === key ? "tabbar-item on" : "tabbar-item"} aria-current={current === key ? "page" : undefined}>
      <TabIcon name={key} />
      {label}
      {badge ? (
        <span className="tabbar-badge" aria-label={`${badge} waiting`}>
          {badge}
        </span>
      ) : null}
    </Link>
  );
  const row = (label: string, link: LinkProps, note?: string) => (
    <Link {...link} className="sheet-row">
      {label}
      {note && <small>{note}</small>}
    </Link>
  );

  return (
    <div className="app">
      <header className="topbar">
        <Mark />
        <button type="button" className="world-chip" aria-haspopup="dialog" aria-expanded={sheet === "world"} onClick={() => setSheet("world")}>
          {worldName}
          <Icon name="chev" />
        </button>
        {/* A page's own phone top bar (the run page's "‹ Runs" and menu) renders here. */}
        <div id="topbar-slot" className="topbar-slot" />
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
            <b>{worldName}</b>
          </span>
          <Icon name="chev" />
        </Link>
        <div className="nav-group">
          {item("gates", "Needs you", { to: "/gates" }, gates?.length)}
          {worldId && (
            <>
              {item("overview", "Overview", { to: "/worlds/$worldId", params })}
              {item("runs", "Runs", { to: "/worlds/$worldId/runs", params })}
              {item("workflows", "Workflows", { to: "/worlds/$worldId/workflows", params })}
              {item("library", "Library", { to: "/worlds/$worldId/library", params })}
              {item("assets", "Assets", { to: "/worlds/$worldId/assets", params })}
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
        <CurrentWorld.Provider value={worldId}>
          <Outlet />
        </CurrentWorld.Provider>
      </div>
      <nav className="tabbar" aria-label="Tabs">
        {tab("gates", "Needs you", { to: "/gates" }, gates?.length)}
        {worldId && (
          <>
            {tab("runs", "Runs", { to: "/worlds/$worldId/runs", params })}
            {tab("workflows", "Workflows", { to: "/worlds/$worldId/workflows", params })}
            {tab("library", "Library", { to: "/worlds/$worldId/library", params })}
          </>
        )}
        <button
          type="button"
          className={MORE.has(current) || sheet === "more" ? "tabbar-item on" : "tabbar-item"}
          aria-haspopup="dialog"
          aria-expanded={sheet === "more"}
          onClick={() => setSheet("more")}
        >
          <TabIcon name="more" />
          More
        </button>
      </nav>
      {/* A row tapped on the page you are on closes its sheet too. */}
      <Sheet open={sheet === "world"} onClose={() => setSheet(null)} label="Worlds">
        <nav aria-label="Worlds" onClick={() => setSheet(null)}>
          {worlds.map((w) => {
            const waiting = gates?.filter((g) => g.world_id === w.id).length ?? 0;
            return (
              <Link
                key={w.id}
                to={SAME_AREA[area as keyof typeof SAME_AREA] ?? "/worlds/$worldId"}
                params={{ worldId: w.id }}
                className="sheet-row"
                aria-current={w.id === worldId ? "true" : undefined}
              >
                {w.name}
                <small>{waiting ? `${waiting} waiting` : "nothing waiting"}</small>
              </Link>
            );
          })}
          {row("All worlds", { to: "/" }, count(worlds.length, "world"))}
        </nav>
      </Sheet>
      <Sheet open={sheet === "more"} onClose={() => setSheet(null)} label="More">
        <nav aria-label="More" onClick={() => setSheet(null)}>
          {worldId && (
            <>
              {row("Overview", { to: "/worlds/$worldId", params }, summary.overview)}
              {row("Assets", { to: "/worlds/$worldId/assets", params }, summary.assets ?? "files your builds read")}
              {row("Memory", { to: "/worlds/$worldId/graph", params }, "graph and notes")}
              {row("Cast", { to: "/worlds/$worldId/cast", params }, summary.cast)}
              {row("Connectors", { to: "/worlds/$worldId/connectors", params }, summary.connectors)}
            </>
          )}
          {row("Evals", { to: "/evals" }, "scorecards")}
          {worldId && row("Settings", { to: "/worlds/$worldId/settings", params }, "this world, keys")}
          {row("All worlds", { to: "/" }, count(worlds.length, "world"))}
        </nav>
        <Health />
      </Sheet>
    </div>
  );
}
