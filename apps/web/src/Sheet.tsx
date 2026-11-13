import { type ReactNode, useEffect, useRef, useState } from "react";

import { usePhone } from "./usePhone";
import { useSheet } from "./useSheet";

/** A sheet that rises from the bottom edge of a phone, over the tab bar. A modal dialog, so focus
 * stays inside it; Escape, a tap on the dimmed backdrop, Android's back, a tap on its grabber or a
 * drag down (useSheet) closes it. Its label heads it unless `head` is false, for a body that has a
 * heading of its own. Rows inside are `.sheet-row` (a label, then a `<small>` summary). */
export function Sheet({
  open,
  onClose,
  label,
  children,
  className = "",
  head = true,
}: {
  open: boolean;
  onClose: () => void;
  label: string;
  children: ReactNode;
  className?: string;
  head?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  // The body stays while the sheet slides out, then goes.
  const [kept, setKept] = useState(open);
  if (open && !kept) setKept(true);
  useEffect(() => {
    if (open || !kept) return;
    const t = setTimeout(() => setKept(false), 360); // --dur-sheet
    return () => clearTimeout(t);
  }, [open, kept]);
  useEffect(() => {
    const dialog = ref.current;
    if (open && !dialog?.open) dialog?.showModal();
    if (!open && dialog?.open) dialog.close();
  }, [open]);
  useSheet(ref, open, onClose, usePhone());
  return (
    // The dialog itself is only hit outside the body, on the backdrop.
    <dialog ref={ref} className={`sheet ${className}`.trim()} aria-label={label} onClose={onClose} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="sheet-grip">
        <button type="button" className="sheet-handle" aria-label="Close" onClick={onClose} />
        {head && <h2 className="sheet-title">{label}</h2>}
      </div>
      {/* Inert while it slides out, so a tap on the leaving sheet does nothing. */}
      <div className="sheet-body" inert={!open}>
        {kept && children}
      </div>
    </dialog>
  );
}
