import { type ReactNode, useState } from "react";

/** One item of a Menu: what it does, and a subtitle naming what happens and where you land. */
export type MenuItem = {
  label: string;
  note: string;
  onSelect: () => void;
  disabled?: boolean;
  danger?: boolean;
};

/** A button that opens a short list of actions. It closes when an item is chosen, on Escape, or
 * when focus leaves it. */
export function Menu({ label, button, items, className }: { label: string; button: ReactNode; items: MenuItem[]; className?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span
      className="menu-wrap"
      onBlur={(e) => !e.currentTarget.contains(e.relatedTarget) && setOpen(false)}
      onKeyDown={(e) => e.key === "Escape" && setOpen(false)}
    >
      <button type="button" className={className} aria-label={label} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>
        {button}
      </button>
      {open && (
        <span className="menu" role="menu" aria-label={label}>
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              className={item.danger ? "danger" : undefined}
              disabled={item.disabled}
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
            >
              {item.label}
              <small>{item.note}</small>
            </button>
          ))}
        </span>
      )}
    </span>
  );
}
