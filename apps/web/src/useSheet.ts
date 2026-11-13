import { type RefObject, useEffect, useRef } from "react";

/** A released sheet closes past this share of its height... */
export const CLOSE_AT = 0.3;
/** ...or when flicked down faster than this, in px per ms. */
export const FLICK = 0.5;

/** Whether a sheet let go `dy` px below where it rests, moving at `velocity` px/ms, closes. */
export function closes(dy: number, height: number, velocity: number): boolean {
  return dy > height * CLOSE_AT || (dy > 0 && velocity > FLICK);
}

/** Where the sheet sits for a finger `dy` px from where it went down: 1:1 downward, a short
 * rubber band upward. */
export function follow(dy: number): number {
  return dy >= 0 ? dy : -12 * Math.log1p(-dy / 12);
}

// Android's back closes the top sheet, not the page: while any sheet is open one history entry
// marked `sheet` sits on top, shared by all of them. It is synced a microtask after sheets open or
// close, so a menu that closes as its confirm opens keeps the entry, and a link that navigated
// away is left alone.
const stack: (() => void)[] = []; // the open sheets' close, topmost last
let queued = false;
let backing = false; // our own back() is on its way
function sync() {
  if (queued) return;
  queued = true;
  queueMicrotask(() => {
    queued = false;
    const ours = !!history.state?.sheet;
    if (stack.length && !ours) history.pushState({ ...history.state, sheet: true }, "");
    else if (!stack.length && ours && !backing) {
      backing = true;
      history.back();
    }
  });
}
if (typeof window !== "undefined")
  addEventListener("popstate", () => {
    if (backing) backing = false;
    else if (!history.state?.sheet) stack.at(-1)?.();
    sync();
  });

/** Native bottom sheet behaviour for a modal `<dialog>` while `shown`:
 * - Android's back gesture closes it (the shared entry above);
 * - with `drag`, it follows a finger down from its `.sheet-grip`, or from anywhere while nothing
 *   under the finger is scrolled, and on release closes or springs back. The backdrop fades with
 *   it through `--sheet-fade`. */
export function useSheet(ref: RefObject<HTMLDialogElement | null>, shown: boolean, close: () => void, drag: boolean) {
  const latest = useRef(close);
  latest.current = close;

  useEffect(() => {
    if (!shown) return;
    const me = () => latest.current();
    stack.push(me);
    sync();
    return () => {
      stack.splice(stack.indexOf(me), 1);
      sync();
    };
  }, [shown]);

  useEffect(() => {
    const el = ref.current;
    if (!shown || !el) return;
    el.style.transform = "";
    el.style.removeProperty("--sheet-fade");
    if (!drag) return;
    let start: { y: number; grip: boolean; pointer: number } | null = null;
    let dragging = false;
    let dragged = false;
    let samples: [number, number][] = [];
    const scrolled = (t: Element | null) => {
      for (let n = t; n && n !== el; n = n.parentElement) if (n.scrollTop > 0) return true;
      return el.scrollTop > 0;
    };
    const down = (e: PointerEvent) => {
      const t = e.target as Element;
      if (e.button !== 0 || t === el || t.closest("input, textarea, select, [contenteditable]")) return;
      const grip = !!t.closest(".sheet-grip");
      // A mouse drags only by the grip; a finger anywhere the content is at its top.
      if (!grip && (e.pointerType === "mouse" || scrolled(t))) return;
      start = { y: e.clientY, grip, pointer: e.pointerId };
      dragging = false;
      samples = [[e.clientY, e.timeStamp]];
    };
    const move = (e: PointerEvent) => {
      if (!start || e.pointerId !== start.pointer) return;
      const dy = e.clientY - start.y;
      if (!dragging) {
        if (Math.abs(dy) < 6) return;
        if (dy < 0 && !start.grip) return void (start = null); // the content scrolls instead
        dragging = true;
        el.setPointerCapture(e.pointerId);
        el.setAttribute("data-dragging", "");
      }
      samples = [...samples.filter(([, t]) => e.timeStamp - t < 100), [e.clientY, e.timeStamp]];
      const y = follow(dy);
      el.style.transform = `translateY(${y}px)`;
      el.style.setProperty("--sheet-fade", String(Math.max(0, 1 - Math.max(0, y) / el.offsetHeight)));
    };
    const up = (e: PointerEvent) => {
      if (!start || e.pointerId !== start.pointer) return;
      const dy = e.clientY - start.y;
      const was = dragging;
      start = null;
      dragging = false;
      if (!was) return;
      dragged = true;
      setTimeout(() => (dragged = false));
      el.removeAttribute("data-dragging");
      const [y0, t0] = samples[0]!;
      const v = (e.clientY - y0) / Math.max(1, e.timeStamp - t0);
      if (e.type === "pointerup" && closes(dy, el.offsetHeight, v)) {
        // Slide the rest of the way from where the finger left it, then close.
        el.style.transform = "translateY(100%)";
        el.style.setProperty("--sheet-fade", "0");
        const ms = matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 260;
        setTimeout(() => latest.current(), ms);
      } else {
        el.style.transform = "";
        el.style.removeProperty("--sheet-fade");
      }
    };
    // Keep the browser from scrolling (and cancelling the pointer) while the sheet is pulled.
    const touch = (e: TouchEvent) => {
      if (dragging || (start && e.touches[0]!.clientY > start.y)) e.preventDefault();
    };
    // A drag that ends over a row does not also tap it.
    const click = (e: MouseEvent) => {
      if (!dragged) return;
      e.preventDefault();
      e.stopPropagation();
    };
    el.addEventListener("pointerdown", down);
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
    el.addEventListener("touchmove", touch, { passive: false });
    el.addEventListener("click", click, true);
    return () => {
      el.removeEventListener("pointerdown", down);
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
      el.removeEventListener("touchmove", touch);
      el.removeEventListener("click", click, true);
      el.removeAttribute("data-dragging");
    };
  }, [ref, shown, drag]);
}
