import { useSyncExternalStore } from "react";

/** The phone layout's breakpoint, the same 48rem the stylesheets use. */
const PHONE = "(max-width: 48rem)";

/** True below 48rem, for screens whose phone layout is different markup, not only CSS. */
export function usePhone(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const query = matchMedia(PHONE);
      query.addEventListener("change", onChange);
      return () => query.removeEventListener("change", onChange);
    },
    () => matchMedia(PHONE).matches,
  );
}
