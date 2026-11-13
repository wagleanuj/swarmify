import type { ReactNode } from "react";

import { Icon } from "./Icon";

/** A page's intro: one sentence under the title, the rest behind an info glyph (a native
 * disclosure), so the first content row stays high on a phone. */
export function Intro({ lead, children, label = "More about this page" }: { lead: ReactNode; children: ReactNode; label?: string }) {
  return (
    <details className="intro">
      <summary>
        <span className="sub">{lead}</span>
        <span className="intro-i" role="img" aria-label={label}>
          <Icon name="info" />
        </span>
      </summary>
      <div className="intro-more">{children}</div>
    </details>
  );
}
