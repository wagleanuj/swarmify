import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";

/** What a Dialog asks: a title, a line per thing that happens, and the action's name. A danger
 * action (abort, delete) is drawn red. */
export type Ask = { title: string; lines: string[]; action: string; danger?: boolean };

/** A modal confirm on the native `<dialog>`: focus stays inside, Escape or a tap on the backdrop
 * cancels, the page behind is inert. Focus starts on Cancel, the safe answer. A centred card on a
 * wide screen, a bottom sheet on a phone. It renders where it is used, so the skin around it
 * (plain or Office) styles it. */
export function Dialog({ ask, onDone }: { ask: Ask; onDone: (ok: boolean) => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const safe = useRef<HTMLButtonElement>(null);
  const id = useId();
  useEffect(() => {
    if (!ref.current?.open) ref.current?.showModal();
    safe.current?.focus();
  }, []);
  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby={`${id}-title`}
      aria-describedby={`${id}-body`}
      onClose={(e) => onDone(e.currentTarget.returnValue === "ok")}
      // The dialog itself is only hit outside the form, on the backdrop.
      onClick={(e) => e.target === e.currentTarget && e.currentTarget.close()}
    >
      <form method="dialog">
        <h2 id={`${id}-title`}>{ask.title}</h2>
        <div id={`${id}-body`} className="dialog-body">
          {ask.lines.map((line) => (
            <p key={line}>{line}</p>
          ))}
        </div>
        <div className="dialog-actions">
          <button ref={safe} value="cancel">
            Cancel
          </button>
          <button value="ok" className={ask.danger ? "danger" : "primary"}>
            {ask.action}
          </button>
        </div>
      </form>
    </dialog>
  );
}

/** `confirm(ask)` resolves true when the action is chosen, false on Cancel, Escape or the
 * backdrop. Render `dialog` where the component renders. */
export function useConfirm(): [(ask: Ask) => Promise<boolean>, ReactNode] {
  const [open, setOpen] = useState<{ ask: Ask; done: (ok: boolean) => void } | null>(null);
  const confirm = useCallback((ask: Ask) => new Promise<boolean>((done) => setOpen({ ask, done })), []);
  const dialog = open && (
    <Dialog
      ask={open.ask}
      onDone={(ok) => {
        setOpen(null);
        open.done(ok);
      }}
    />
  );
  return [confirm, dialog];
}
