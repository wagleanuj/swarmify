import type { ReactNode } from "react";

import { Icon, type IconName } from "./Icon";

/** A place with nothing in it yet: the place's glyph, which rises in once, one sentence saying
 * what lands here, and the one action that fills it. `title` only where a heading is the news
 * (All clear). Render it only after the first response: a skeleton holds the place until then. */
export function Empty({
  icon,
  title,
  children,
  action,
  testId,
  popped = false,
}: {
  icon: IconName;
  /** The place just emptied by the user's own hand (the last gate answered): its check pops. */
  popped?: boolean;
  title?: ReactNode;
  children: ReactNode;
  action: ReactNode;
  testId?: string;
}) {
  return (
    <div className="empty empty-state" data-testid={testId}>
      <Icon name={icon} size={24} className={popped ? "i-pop" : "i-enter"} />
      {title && <h2>{title}</h2>}
      <p>{children}</p>
      {action}
    </div>
  );
}
