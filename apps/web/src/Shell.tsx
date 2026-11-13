import { Link, type LinkProps, Outlet, useLocation } from "@tanstack/react-router";
import { createContext, Fragment, useEffect, useLayoutEffect, useRef, useState } from "react";

import { api } from "./api/client";
import { needsYou, useOpenGates } from "./api/gates";
import { money } from "./api/runs";
import type { components } from "./api/schema";
import { CountTick } from "./CountTick";
import { Health } from "./Health";
import { Icon, type IconName } from "./Icon";
import { status } from "./pages/Connectors";
import { figures } from "./pages/WorldOverview";
import { Sheet } from "./Sheet";
import { Skeleton } from "./Skeleton";
import { worldHome } from "./skin";
import "./styles/shell.css";

type World = components["schemas"]["World"];

// Each area's glyph, in the sidebar and on the phone's tab bar.
const NAV_ICON = {
  gates: "inbox",
  overview: "home",
  runs: "runs",
  workflows: "workflows",
  library: "library",
  assets: "assets",
  graph: "memory",
  cast: "people",
  connectors: "plug",
  settings: "settings",
  worlds: "world",
  evals: "evals",
  more: "more",
} as const satisfies Record<string, IconName>;
type NavKey = keyof typeof NAV_ICON;
// The world's own tabs, between Needs you and More.
const WORLD_TABS = [
  ["runs", "Runs", "/worlds/$worldId/runs"],
  ["workflows", "Workflows", "/worlds/$worldId/workflows"],
  ["library", "Library", "/worlds/$worldId/library"],
] as const;
// Areas reached through More, so its tab is current on them.
const MORE = new Set(["overview", "assets", "graph", "cast", "connectors", "settings", "worlds", "evals"]);
// Switching worlds keeps the area you are in; anywhere else lands where the world opens.
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
// The last world opened, so the next cold load names it and links its tabs before the worlds load.
const LAST = "swarmify.world";
const stored = (): Pick<World, "id" | "name"> | undefined => {
  try {
    return JSON.parse(localStorage.getItem(LAST) ?? "null") ?? undefined;
  } catch {
    return undefined;
  }
};
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

/** The app shell: a labelled sidebar around every screen; on a phone, a world chip on top and a
 * bottom tab bar with More instead. */
export function Shell() {
  const { pathname, hash } = useLocation();
  // `/worlds/<id>/<area>`, or a global route: `/` (all worlds), `/gates`, `/evals`.
  const [, top, routeWorld, area] = pathname.split("/");
  const current = top === "worlds" ? (area ?? "overview") : top || "worlds";
  const inWorld = top === "worlds" ? routeWorld : undefined;

  // The inbox glyph nudges once each time the count of open gates rises (keyed, so it replays).
  const gates = useOpenGates().gates;
  const waiting = needsYou(gates) ?? undefined;
  const [nudge, setNudge] = useState({ was: waiting, n: 0 });
  if (waiting !== nudge.was) setNudge({ was: waiting, n: waiting !== undefined && nudge.was !== undefined && waiting > nudge.was ? nudge.n + 1 : nudge.n });
  const inbox = (size?: 24) => <Icon key={nudge.n} name="inbox" size={size} className={nudge.n ? "i-nudge" : undefined} />;
  const [worlds, setWorlds] = useState<World[] | null>(null);
  // Global routes keep the last world the person was in, across visits; before any, the first one.
  const [saved] = useState(stored);
  const [last, setLast] = useState<string | undefined>(inWorld ?? saved?.id);
  if (inWorld && inWorld !== last) setLast(inWorld);
  // A remembered world that is gone gives way once the worlds load.
  const lastLive = last && (!worlds || worlds.some((w) => w.id === last)) ? last : undefined;
  const worldId = inWorld ?? lastLive ?? worlds?.[0]?.id;
  const [sheet, setSheet] = useState<"world" | "more" | null>(null);
  const [summary, setSummary] = useState<Record<string, string>>({});

  // Refetch on every world change, so a world made a moment ago has its name.
  useEffect(() => {
    api
      .GET("/api/worlds")
      .then(({ data }) => data && setWorlds(data))
      .catch(() => undefined);
  }, [worldId]);
  useEffect(() => setSheet(null), [pathname, hash]);

  // The compact title: once the page's own title has scrolled under the top bar, its text fades
  // into the bar, so a long page never loses where it is.
  const [title, setTitle] = useState<string | null>(null);
  const topbar = useRef<HTMLElement>(null);
  useEffect(() => {
    const check = () => {
      const h1 = document.querySelector(".canvas h1");
      const bar = topbar.current?.getBoundingClientRect().bottom ?? 0;
      setTitle(h1 && bar > 0 && h1.getBoundingClientRect().bottom < bar ? (h1.textContent ?? "").trim() || null : null);
    };
    check();
    addEventListener("scroll", check, { passive: true });
    return () => removeEventListener("scroll", check);
  }, [pathname]);

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

  const item = (key: NavKey, label: string, link: LinkProps, count?: number) => (
    <Link
      {...link}
      className="nav-item"
      // Every Link marks itself current on an exact match; `current` covers the rest.
      activeOptions={{ exact: true, includeHash: true }}
      aria-current={current === key ? "page" : undefined}
    >
      {key === "gates" ? inbox() : <Icon name={NAV_ICON[key]} />}
      <span className="nav-label">{label}</span>
      {count ? (
        <span className="nav-count" aria-label={`${count} waiting`}>
          <CountTick value={count} />
        </span>
      ) : null}
    </Link>
  );
  const params = { worldId: worldId ?? "" };
  const world = worlds?.find((w) => w.id === worldId);
  useEffect(() => {
    if (world) localStorage.setItem(LAST, JSON.stringify({ id: world.id, name: world.name }));
  }, [world]);
  // Until the worlds load: the remembered name, else a placeholder the size of a name.
  const known = world?.name ?? (worldId && worldId === saved?.id ? saved.name : undefined);
  const worldName = known ?? (worlds ? "Choose a world" : undefined);

  // The selection line sits over the current tab's label and slides to the next one.
  const bar = useRef<HTMLElement>(null);
  const moreOn = MORE.has(current) || sheet === "more";
  useLayoutEffect(() => {
    const nav = bar.current;
    if (!nav) return;
    const place = () => {
      const label = nav.querySelector(".tabbar-item.on .tabbar-label");
      if (!label) return nav.style.setProperty("--line-w", "0px");
      const [n, l] = [nav.getBoundingClientRect(), label.getBoundingClientRect()];
      nav.style.setProperty("--line-x", `${l.left - n.left}px`);
      nav.style.setProperty("--line-w", `${l.width}px`);
      // Attached to the tab: just under its label, as the sidebar underlines its current row.
      nav.style.setProperty("--line-y", `${l.bottom - n.top + 2}px`);
    };
    place();
    // Slide from the next frame on, so the first placement does not glide in from the left.
    const frame = requestAnimationFrame(() => nav.setAttribute("data-slide", ""));
    addEventListener("resize", place);
    // A web font weight loads on first use (the bold of a newly current tab) and re-sizes the label.
    document.fonts?.addEventListener("loadingdone", place);
    return () => {
      cancelAnimationFrame(frame);
      removeEventListener("resize", place);
      document.fonts?.removeEventListener("loadingdone", place);
    };
  }, [current, moreOn, worldId]);

  const tab = (key: NavKey, label: string, link: LinkProps, badge?: number) => (
    // While More's sheet is open it is the one tab marked on.
    <Link {...link} className={current === key && sheet !== "more" ? "tabbar-item on" : "tabbar-item"} aria-current={current === key ? "page" : undefined}>
      {key === "gates" ? inbox(24) : <Icon name={NAV_ICON[key]} size={24} />}
      <span className="tabbar-label">{label}</span>
      {badge ? (
        <span className="tabbar-badge" aria-label={`${badge} waiting`}>
          <CountTick value={badge} />
        </span>
      ) : null}
    </Link>
  );
  const row = (label: string, link: LinkProps, note?: string, icon?: NavKey) => (
    <Link {...link} className="sheet-row">
      {icon ? (
        <span className="sheet-lead">
          <Icon name={NAV_ICON[icon]} size={20} />
          {label}
        </span>
      ) : (
        label
      )}
      {note && <small>{note}</small>}
    </Link>
  );

  return (
    <div className="app">
      <header className="topbar" ref={topbar} data-titled={title ? "" : undefined}>
        <Mark />
        <button type="button" className="world-chip" aria-label={worldName ? undefined : "Choose a world"} aria-haspopup="dialog" aria-expanded={sheet === "world"} onClick={() => setSheet("world")}>
          {worldName ?? <Skeleton w={96} h={14} />}
          <Icon name="chevron" />
        </button>
        <span className="topbar-title" aria-hidden="true">
          {title}
        </span>
        {/* A page's own phone top bar (the run page's back link and menu) renders here. */}
        <div id="topbar-slot" className="topbar-slot" />
      </header>
      <nav className="sidebar" aria-label="Main">
        <div className="sidebar-head">
          <Mark />
        </div>
        <Link to="/" className="switcher" activeOptions={{ exact: true, includeHash: true }} data-testid="world-switcher">
          <span>
            <small>World</small>
            <b>{worldName ?? <Skeleton w={96} h={13} />}</b>
          </span>
          <Icon name="chevron" />
        </Link>
        <div className="nav-group">
          {item("gates", "Needs you", { to: "/gates" }, waiting)}
          {worldId && (
            <>
              {item("overview", "Overview", { to: worldHome(world), params })}
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
      <nav className="tabbar" aria-label="Tabs" ref={bar}>
        <span className="tabbar-line" aria-hidden="true" />
        {tab("gates", "Needs you", { to: "/gates" }, waiting)}
        {WORLD_TABS.map(([key, label, to]) =>
          worldId ? (
            <Fragment key={key}>{tab(key, label, { to, params })}</Fragment>
          ) : (
            // Five slots from the first frame: the world's tabs wait, labelled, until there is a world.
            <span key={key} className="tabbar-item" aria-disabled="true">
              <Icon name={NAV_ICON[key]} size={24} />
              <span className="tabbar-label">{label}</span>
            </span>
          ),
        )}
        <button type="button" className={moreOn ? "tabbar-item on" : "tabbar-item"} aria-haspopup="dialog" aria-expanded={sheet === "more"} onClick={() => setSheet("more")}>
          <Icon name="more" size={24} />
          <span className="tabbar-label">More</span>
        </button>
      </nav>
      {/* A row tapped on the page you are on closes its sheet too. */}
      <Sheet open={sheet === "world"} onClose={() => setSheet(null)} label="Worlds">
        <nav aria-label="Worlds" onClick={() => setSheet(null)}>
          {(worlds ?? []).map((w) => {
            const waiting = needsYou(gates, w.id);
            return (
              <Link
                key={w.id}
                to={SAME_AREA[area as keyof typeof SAME_AREA] ?? worldHome(w)}
                params={{ worldId: w.id }}
                className="sheet-row"
                aria-current={w.id === worldId ? "true" : undefined}
              >
                {w.name}
                <small>{waiting === null ? "" : waiting ? `${waiting} waiting` : "nothing waiting"}</small>
              </Link>
            );
          })}
          {row("All worlds", { to: "/" }, count(worlds?.length ?? 0, "world"))}
        </nav>
      </Sheet>
      <Sheet open={sheet === "more"} onClose={() => setSheet(null)} label="More">
        <nav aria-label="More" className="stagger" onClick={() => setSheet(null)}>
          {worldId && (
            <>
              {row("Overview", { to: worldHome(world), params }, summary.overview, "overview")}
              {row("Assets", { to: "/worlds/$worldId/assets", params }, summary.assets ?? "files your builds read", "assets")}
              {row("Memory", { to: "/worlds/$worldId/graph", params }, "graph and notes", "graph")}
              {row("Cast", { to: "/worlds/$worldId/cast", params }, summary.cast, "cast")}
              {row("Connectors", { to: "/worlds/$worldId/connectors", params }, summary.connectors, "connectors")}
            </>
          )}
          {row("Evals", { to: "/evals" }, "scorecards", "evals")}
          {worldId && row("Settings", { to: "/worlds/$worldId/settings", params }, "this world, keys", "settings")}
          {row("All worlds", { to: "/" }, count(worlds?.length ?? 0, "world"), "worlds")}
        </nav>
        <Health />
      </Sheet>
    </div>
  );
}
