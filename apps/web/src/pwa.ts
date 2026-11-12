/** Swarmify as an installed app (issue 458): the service worker and its update, the browser's
 * install offer, the theme colour per view and whether the person has been given something first.
 * One small store; PwaNotices.tsx draws it. */
import { useEffect, useSyncExternalStore } from "react";

/** Chrome's install offer, held until the person asks for it. */
export type InstallOffer = Event & { prompt(): Promise<void> };

type State = {
  /** A new version installed and waiting for the person to reload into it. */
  waiting: ServiceWorker | null;
  offer: InstallOffer | null;
  online: boolean;
  /** The Office draws its own bottom chrome, so the phone prompt stays off it. */
  office: boolean;
};
let state: State = { waiting: null, offer: null, online: true, office: false };
const listeners = new Set<() => void>();
const set = (next: Partial<State>) => {
  state = { ...state, ...next };
  listeners.forEach((l) => l());
};
const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => listeners.delete(l);
};
export const usePwa = () => useSyncExternalStore(subscribe, () => state);
/** For tests: put the store in a known state. */
export const setPwa = set;

/** The ground behind the browser's bars: the plain theme's ground, or the Office sky. */
export const THEME_COLOUR = { plain: "#171719", office: "#7cc8ff" } as const;

/** Keep `<meta name="theme-color">` on the view the person is in. */
export function useThemeColour(office: boolean) {
  useEffect(() => {
    for (const meta of document.querySelectorAll('meta[name="theme-color"]')) meta.setAttribute("content", THEME_COLOUR[office ? "office" : "plain"]);
    if (state.office !== office) set({ office });
  }, [office]);
}

const KEY = { days: "swarmify.days", answered: "swarmify.answered", install: "swarmify.install" };
const read = (key: string) => {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
};
const write = (key: string, value: string) => {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Private mode: the hint just asks again next time.
  }
};

const days = (): string[] => {
  try {
    const value: unknown = JSON.parse(read(KEY.days) ?? "[]");
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
};

/** Gate answered: the app has given something, so it may offer itself. */
export const markAnswered = () => write(KEY.answered, "1");
/** The phone prompt, dismissed or used: never shown again (Settings still offers it). */
export const dismissInstall = () => write(KEY.install, "done");

/** Opened on three different days, or a gate answered, and the prompt never dismissed. */
export function earnedInstallHint(): boolean {
  if (read(KEY.install)) return false;
  return days().length >= 3 || read(KEY.answered) === "1";
}

const countToday = () => {
  const today = new Date().toISOString().slice(0, 10);
  const seen = days();
  if (!seen.includes(today)) write(KEY.days, JSON.stringify([...seen, today].slice(-3)));
};

export const standalone = () =>
  matchMedia("(display-mode: standalone)").matches || (navigator as { standalone?: boolean }).standalone === true;
/** iOS has no install offer: the person adds it from Safari's Share sheet. */
export const ios = () =>
  /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

/** Ask Chrome for its install dialog; the offer is spent either way. */
export async function install() {
  const offer = state.offer;
  set({ offer: null });
  await offer?.prompt();
}

let reloading = false;
/** Switch to the waiting version: it takes over, then the page reloads once into it. */
export function update() {
  reloading = true;
  state.waiting?.postMessage("SKIP_WAITING");
}

/** Called once from main.tsx, before the app draws. */
export function startPwa() {
  countToday();
  set({ online: navigator.onLine });
  addEventListener("online", () => set({ online: true }));
  addEventListener("offline", () => set({ online: false }));
  addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    set({ offer: event as InstallOffer });
  });
  addEventListener("appinstalled", () => set({ offer: null }));

  // Only the built app has /sw.js; the dev server would cache nothing useful.
  if (!import.meta.env.PROD || !("serviceWorker" in navigator)) return;
  const sw = navigator.serviceWorker;
  sw.addEventListener("controllerchange", () => {
    if (reloading) location.reload();
  });
  sw.register("/sw.js", { scope: "/" })
    .then((reg) => {
      // A first install is not an update: there is no page on an older version to tell.
      const offer = (worker: ServiceWorker | null) => worker && sw.controller && set({ waiting: worker });
      offer(reg.waiting);
      reg.addEventListener("updatefound", () => {
        const worker = reg.installing;
        worker?.addEventListener("statechange", () => worker.state === "installed" && offer(worker));
      });
      // A phone keeps the app open for days: look for a new build each time it comes back.
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") reg.update().catch(() => undefined);
      });
    })
    .catch(() => undefined);
}
