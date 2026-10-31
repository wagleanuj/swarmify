import { Link } from "@tanstack/react-router";
import { useEffect, useState, useSyncExternalStore } from "react";

import { useOpenGates } from "../api/gates";
import { line } from "./scene";

// Day and night are this browser's choice, never the world's.
const NIGHT = "swarmify.office.night";
const listeners = new Set<() => void>();

/** Night or day in this browser, the same on every office screen and tab. */
export function useNight(): [boolean, () => void] {
  const night = useSyncExternalStore(
    (onChange) => {
      listeners.add(onChange);
      window.addEventListener("storage", onChange);
      return () => {
        listeners.delete(onChange);
        window.removeEventListener("storage", onChange);
      };
    },
    () => localStorage.getItem(NIGHT) === "1",
  );
  const toggle = () => {
    if (night) localStorage.removeItem(NIGHT);
    else localStorage.setItem(NIGHT, "1");
    for (const l of listeners) l();
  };
  return [night, toggle];
}

/** The root class of an office screen, dark at night. */
export const appClass = (night: boolean, extra = "") => ["office-app", extra, night ? "office-night" : ""].filter(Boolean).join(" ");

/** Full screen for a spare-screen display. Browsers without it (iPhone Safari) get no button. */
export function FullScreen() {
  const [on, setOn] = useState(() => !!document.fullscreenElement);
  useEffect(() => {
    const follow = () => setOn(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", follow);
    return () => document.removeEventListener("fullscreenchange", follow);
  }, []);
  if (!document.fullscreenEnabled) return null;
  return (
    <button
      type="button"
      className="office-btn"
      aria-pressed={on}
      onClick={() => void (on ? document.exitFullscreen() : document.documentElement.requestFullscreen()).catch(() => undefined)}
    >
      Full screen
    </button>
  );
}

/** The office's tabs: a bar along the bottom of a phone; on a wide screen only day and night
 * show, the top bars carry the rest. */
export function Tabs({ worldId }: { worldId: string }) {
  const waiting = line(useOpenGates().gates, worldId).length;
  const [night, toggle] = useNight();
  const params = { worldId };
  return (
    <nav className="office-tabs" aria-label="Office tabs">
      <Link className="office-btn office-tab" to="/worlds/$worldId/office" params={params} activeOptions={{ exact: true }}>
        Office
      </Link>
      <Link className="office-btn office-tab" to="/worlds/$worldId/office/talk" params={params} aria-label={`Needs you ${waiting} waiting`}>
        Needs you <span className="office-count">{waiting}</span>
      </Link>
      <Link className="office-btn office-tab" to="/worlds/$worldId/office/crew" params={params}>
        Crew
      </Link>
      <button type="button" className="office-btn" aria-pressed={night} onClick={toggle}>
        Night
      </button>
    </nav>
  );
}
