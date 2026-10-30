import { type ReactNode, useEffect, useRef } from "react";

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
  useEffect(() => {
    const dialog = ref.current;
    if (open && !dialog?.open) dialog?.showModal();
    if (!open && dialog?.open) dialog.close();
  }, [open]);
  return (
    // The dialog itself is only hit outside the body, on the backdrop.
    <dialog ref={ref} className={`sheet ${className}`.trim()} aria-label={label} onClose={onClose} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="sheet-body">{open && children}</div>
    </dialog>
  );
}
