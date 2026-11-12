import { type ReactNode, useState } from "react";

import { Sheet } from "../Sheet";
import { usePhone } from "../usePhone";

/** One item of a Menu: what it does, and a subtitle naming what happens and where you land. */
export type MenuItem = {
  label: string;
  note: string;
  onSelect: () => void;
  disabled?: boolean;
  danger?: boolean;
};

/** A button that opens a short list of actions under it. It closes when an item is chosen, on
 * Escape, or when focus leaves it. With `sheet`, a phone gets a sheet from the bottom instead,
 * for a menu in the top bar that would otherwise cover the title it acts on; a tap outside
 * closes it too. */
export function Menu({ label, button, items, className, sheet }: { label: string; button: ReactNode; items: MenuItem[]; className?: string; sheet?: boolean }) {
  const [open, setOpen] = useState(false);
  const phone = usePhone() && sheet;
  const list = (row?: string) =>
    items.map((item) => (
      <button
        key={item.label}
        type="button"
        role="menuitem"
        className={[row, item.danger ? "danger" : ""].filter(Boolean).join(" ") || undefined}
        disabled={item.disabled}
        onClick={() => {
          setOpen(false);
          item.onSelect();
        }}
      >
        {item.label}
        <small>{item.note}</small>
      </button>
    ));
  return (
    <span
      className="menu-wrap"
      onBlur={(e) => !e.currentTarget.contains(e.relatedTarget) && setOpen(false)}
      onKeyDown={(e) => e.key === "Escape" && setOpen(false)}
    >
      <button type="button" className={className} aria-label={label} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>
        {button}
      </button>
      {phone ? (
        <Sheet open={open} onClose={() => setOpen(false)} label={label} className="menu-sheet">
          <div role="menu" aria-label={label}>
            {list("sheet-row")}
          </div>
        </Sheet>
      ) : (
        open && (
          <span className="menu" role="menu" aria-label={label}>
            {list()}
          </span>
        )
      )}
    </span>
  );
}
