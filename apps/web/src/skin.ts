import { api } from "./api/client";

/** Role names on the page (ADR 0004, amended): code says planner and worker-1, the plain skin
 * says Planner and Worker 1. The Pied Piper names are an opt-in local skin, turned on in
 * Settings and kept in this browser only. */
const KEY = "swarmify.skin";

const PLAIN: Record<string, string> = {
  planner: "Planner",
  judge: "Judge",
  retro: "Retro",
  writer: "Graph writer",
  budget: "Budget",
};
const PIED_PIPER: Record<string, string> = {
  planner: "Richard",
  judge: "Erlich",
  "worker-1": "Gilfoyle",
  "worker-2": "Dinesh",
  "worker-3": "Jared",
  "worker-4": "Big Head",
  writer: "Anton",
  budget: "Russ",
};

export const piedPiper = () => localStorage.getItem(KEY) === "pied-piper";

export function setPiedPiper(on: boolean) {
  if (on) localStorage.setItem(KEY, "pied-piper");
  else localStorage.removeItem(KEY);
}

/** A role or agent as a label: Planner, Worker 2, Judge (or Richard, Dinesh, Erlich). */
export function role(name: string): string {
  if (piedPiper() && PIED_PIPER[name]) return PIED_PIPER[name];
  const worker = /^worker-(\d+)$/.exec(name);
  return worker ? `Worker ${worker[1]}` : (PLAIN[name] ?? name);
}

/** A role mid-sentence: "the judge" (or "Erlich"). `cap` it to start a sentence. */
export const the = (name: string) => (piedPiper() && PIED_PIPER[name] ? PIED_PIPER[name] : `the ${role(name).toLowerCase()}`);

/** A baseline run mid-sentence: "baseline" (or "Hooli baseline"). */
export const baseline = () => (piedPiper() ? "Hooli baseline" : "baseline");

export const cap = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/** How a world is drawn: the plain pages or the Office skin, saved on the world. */
export type View = "plain" | "office";

/** Where a world opens: its office when its view is Office, else the plain overview. */
export const worldHome = (world?: { skin?: { view: View } }) =>
  world?.skin?.view === "office" ? ("/worlds/$worldId/office" as const) : ("/worlds/$worldId" as const);

/** Save the world's view; false when the API refused it. */
export async function saveView(worldId: string, view: View): Promise<boolean> {
  const { error } = await api.PATCH("/api/worlds/{world_id}", {
    params: { path: { world_id: worldId } },
    body: { skin: { view } },
  });
  return !error;
}
