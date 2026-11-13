import { notify } from "./Toast";

/** The success moments, the only times the app says well done: each is a notice whose check
 * pops in (Toast.tsx), never confetti, never an emoji. Firsts are told once; the rest every time. */
export const MOMENTS = {
  firstRun: "Your first run is on its way. Each stage lands on its page as it finishes.",
  firstGate: "Your first decision is in. A run that needs you again waits in Needs you.",
  runDone: (name: string) => `${name} is done.`,
  workflowSaved: (version: number) => `Saved as version ${version}.`,
} as const;

const ANSWERED = "swarmify.answered-a-gate";

/** Whether this browser has answered a gate before; marks it answered. */
export function firstGate(): boolean {
  try {
    if (localStorage.getItem(ANSWERED)) return false;
    localStorage.setItem(ANSWERED, "1");
    return true;
  } catch {
    return false;
  }
}

/** Tell a success moment. */
export const moment = (text: string, action?: { label: string; run: () => void }) => notify(text, { tone: "ok", action });
