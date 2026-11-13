import { useSyncExternalStore } from "react";

/** The phone layout's breakpoint, the same 48rem the stylesheets use. */
const PHONE = "(max-width: 48rem)";

/** True below 48rem (or the `query` a skin breaks at), for screens whose phone layout is different
 * markup, not only CSS. */
export function usePhone(query = PHONE): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const list = matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    () => matchMedia(query).matches,
  );
}
