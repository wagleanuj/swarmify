import { type PointerEvent, useEffect, useRef, useSyncExternalStore } from "react";

import { Icon } from "./Icon";

/** A short notice after a save, an answer, a copy or an error. */
export type Notice = {
  id: number;
  text: string;
  /** An error stays until dismissed; the others go after 4 s. */
  tone?: "ok" | "error";
  /** One way on: Undo, Open. Pressing it also dismisses the notice. */
  action?: { label: string; run: () => void };
};

const STAY_MS = 4000;
const MAX = 2;
let notices: Notice[] = [];
let nextId = 1;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

/** Show a notice. The newest sits nearest the tab bar; a third pushes the oldest out. */
export function notify(text: string, opts: Omit<Notice, "id" | "text"> = {}): number {
  const id = nextId++;
  notices = [...notices, { id, text, ...opts }].slice(-MAX);
  emit();
  return id;
}

export function dismiss(id: number) {
  notices = notices.filter((n) => n.id !== id);
  emit();
}

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => listeners.delete(l);
};

/** The one polite live region, above the tab bar and the safe area, in the plain shell and in
 * the Office (which draws each notice as a speech bubble). */
export function Toasts() {
  const list = useSyncExternalStore(subscribe, () => notices);
  return (
    <div className="toasts" aria-live="polite">
      {list.map((n) => (
        <Toast key={n.id} n={n} />
      ))}
    </div>
  );
}

function Toast({ n }: { n: Notice }) {
  const ref = useRef<HTMLDivElement>(null);
  const start = useRef<number | null>(null);
  useEffect(() => {
    if (n.tone === "error") return;
    const t = setTimeout(() => dismiss(n.id), STAY_MS);
    return () => clearTimeout(t);
  }, [n]);

  // A sideways swipe of 60px dismisses it; a shorter one springs back.
  const move = (e: PointerEvent) => {
    if (start.current === null || !ref.current) return;
    const dx = e.clientX - start.current;
    ref.current.style.transform = `translateX(${dx}px)`;
    ref.current.style.opacity = String(1 - Math.min(Math.abs(dx) / 160, 0.8));
  };
  const up = (e: PointerEvent) => {
    if (start.current === null || !ref.current) return;
    const dx = e.clientX - start.current;
    start.current = null;
    if (Math.abs(dx) > 60) return dismiss(n.id);
    ref.current.style.transform = "";
    ref.current.style.opacity = "";
  };

  return (
    <div
      ref={ref}
      className={`toast${n.tone ? ` toast-${n.tone}` : ""}`}
      role={n.tone === "error" ? "alert" : undefined}
      onPointerDown={(e) => (start.current = e.clientX)}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={up}
    >
      {n.tone === "ok" && <Icon name="check" className="i-pop" />}
      {n.tone === "error" && <Icon name="warning" />}
      <span className="toast-text">{n.text}</span>
      {n.action && (
        <button
          type="button"
          className="toast-action"
          onClick={() => {
            n.action?.run();
            dismiss(n.id);
          }}
        >
          {n.action.label}
        </button>
      )}
      <button type="button" className="toast-close icon" aria-label="Dismiss" onClick={() => dismiss(n.id)}>
        <Icon name="x" />
      </button>
    </div>
  );
}
