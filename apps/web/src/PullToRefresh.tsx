import { useEffect, useRef, useState } from "react";

import { usePwa } from "./pwa";
import { refresh } from "./refresh";
import "./styles/pull.css";

/** How far the spinner drops before a release refreshes, and the most it ever drops, in px. */
export const PULL = { threshold: 72, max: 128 } as const;
/** Where the spinner rests while the page refreshes. */
const REST = 56;

/** Where the spinner sits for a finger `dy` px below where it went down: half the finger's travel
 * up to the threshold, then a stiffer pull, never past `max` (Android's SwipeRefreshLayout). */
export function pullDistance(dy: number): number {
  if (dy <= 0) return 0;
  const d = dy * 0.5;
  return d <= PULL.threshold ? d : Math.min(PULL.max, PULL.threshold + (d - PULL.threshold) * 0.4);
}

/** A release at this distance refreshes. */
export const armed = (distance: number) => distance >= PULL.threshold;

/** What a finger that moved `dx`, `dy` from where it went down is doing: a pull down, something
 * else (a sideways swipe, a scroll up), or not yet clear. */
export function intent(dx: number, dy: number): "pull" | "other" | null {
  if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return null;
  return dy > 0 && dy > Math.abs(dx) ? "pull" : "other";
}

/** A finger here scrolls or edits something of its own: a sheet, a field, an inner scroller that
 * is not at its top. */
function owned(target: Element | null): boolean {
  if (!target || target.closest("dialog, textarea, [contenteditable], [data-no-pull]")) return true;
  for (let n: Element | null = target; n && n !== document.body; n = n.parentElement) if (n.scrollTop > 0) return true;
  return false;
}

const calm = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
type Phase = "idle" | "pulling" | "refreshing" | "settling";

/** Pull to refresh, Android style: from the top of the page a circular spinner follows the
 * finger down; let go past the threshold and it spins while the page refetches its data and
 * reopens its event stream (refresh.ts), then rises away. Never a reload. */
export function PullToRefresh() {
  const { office } = usePwa();
  const spinner = useRef<HTMLDivElement>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [said, setSaid] = useState("");
  const busy = useRef(false);

  useEffect(() => {
    let start: { x: number; y: number } | null = null;
    let pulling = false;
    let at = 0;
    const place = (d: number) => {
      at = d;
      const el = spinner.current;
      if (!el) return;
      el.style.setProperty("--pull", `${d}px`);
      el.style.setProperty("--pull-share", String(Math.min(1, d / PULL.threshold)));
    };
    const down = (e: TouchEvent) => {
      start = null;
      if (busy.current || e.touches.length !== 1 || (document.scrollingElement?.scrollTop ?? 0) > 0) return;
      if (owned(e.target as Element)) return;
      start = { x: e.touches[0]!.clientX, y: e.touches[0]!.clientY };
      pulling = false;
    };
    const move = (e: TouchEvent) => {
      if (!start) return;
      const t = e.touches[0]!;
      const dx = t.clientX - start.x;
      const dy = t.clientY - start.y;
      if (!pulling) {
        const what = intent(dx, dy);
        if (what === null) return;
        if (what === "other") return void (start = null);
        pulling = true;
        setPhase("pulling");
        setSaid("Pull to refresh");
      }
      e.preventDefault();
      const was = armed(at);
      place(pullDistance(dy));
      if (armed(at) !== was) {
        setSaid(armed(at) ? "Release to refresh" : "Pull to refresh");
        if (armed(at) && "vibrate" in navigator) navigator.vibrate(10);
      }
    };
    const up = () => {
      if (!start || !pulling) return void (start = null);
      start = null;
      pulling = false;
      if (!armed(at)) {
        setSaid("");
        setPhase("settling");
        place(0);
        return;
      }
      busy.current = true;
      setPhase("refreshing");
      setSaid("Refreshing");
      place(REST);
      // A turn of the spinner at least, so a fast answer does not flicker.
      const least = new Promise((r) => setTimeout(r, calm() ? 0 : 600));
      void Promise.all([refresh(), least]).then(() => {
        setSaid(navigator.onLine ? "Updated just now" : "You are offline. This is what was last loaded.");
        setPhase("settling");
        place(0);
        busy.current = false;
        // Said, then gone, so the page's own status lines are the only ones left.
        setTimeout(() => setSaid((now) => (now.startsWith("Updated") || now.startsWith("You are offline") ? "" : now)), 4000);
      });
    };
    addEventListener("touchstart", down, { passive: true });
    addEventListener("touchmove", move, { passive: false });
    addEventListener("touchend", up);
    addEventListener("touchcancel", up);
    return () => {
      removeEventListener("touchstart", down);
      removeEventListener("touchmove", move);
      removeEventListener("touchend", up);
      removeEventListener("touchcancel", up);
    };
  }, []);

  return (
    <>
      <div
        ref={spinner}
        className={`ptr ${phase}${office ? " on-office" : ""}`}
        data-testid="pull-to-refresh"
        aria-hidden="true"
        onTransitionEnd={() => setPhase((p) => (p === "settling" ? "idle" : p))}
      >
        <svg viewBox="0 0 24 24" width="24" height="24">
          <circle cx="12" cy="12" r="8" pathLength="100" />
        </svg>
      </div>
      {said && (
        <p role="status" className="visually-hidden">
          {said}
        </p>
      )}
    </>
  );
}
