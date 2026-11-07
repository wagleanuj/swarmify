import { type ReactNode, useEffect, useRef, useState } from "react";

/** A sheet that rises from the bottom of a phone above the tab bar. A modal dialog, so focus
 * stays inside it; Escape or a tap on the dimmed backdrop closes it. Rows inside are
 * `.sheet-row` (a label, then a `<small>` summary). */
export function Sheet({
  open,
  onClose,
  label,
  children,
  className = "",
}: {
  open: boolean;
  onClose: () => void;
  label: string;
  children: ReactNode;
  className?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  // The body stays while the sheet slides out, then goes.
  const [kept, setKept] = useState(open);
  if (open && !kept) setKept(true);
  useEffect(() => {
    if (open || !kept) return;
    const t = setTimeout(() => setKept(false), 320); // --dur-slow
    return () => clearTimeout(t);
  }, [open, kept]);
  useEffect(() => {
    const dialog = ref.current;
    if (open && !dialog?.open) dialog?.showModal();
    if (!open && dialog?.open) dialog.close();
  }, [open]);
  return (
    // The dialog itself is only hit outside the body, on the backdrop.
    <dialog ref={ref} className={`sheet ${className}`.trim()} aria-label={label} onClose={onClose} onClick={(e) => e.target === e.currentTarget && onClose()}>
      {/* Inert while it slides out, so a tap on the leaving sheet does nothing. */}
      <div className="sheet-body" inert={!open}>
        {kept && children}
      </div>
    </dialog>
  );
}
