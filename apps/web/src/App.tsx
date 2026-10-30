import { RouterProvider } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api, SIGNED_OUT } from "./api/client";
import { SignIn } from "./pages/SignIn";
import { createAppRouter } from "./router";

const router = createAppRouter();

/** The app behind the operator's sign-in: the sign-in page until the API says signed in. */
export function App() {
  const [state, setState] = useState<"checking" | "in" | "out">("checking");

  useEffect(() => {
    const out = () => setState("out");
    window.addEventListener(SIGNED_OUT, out);
    api
      .GET("/api/session")
      .then(({ data }) => setState(data?.signed_in === false ? "out" : "in"))
      // An API that does not answer is the sidebar's health line to report, not a sign-in.
      .catch(() => setState("in"));
    return () => window.removeEventListener(SIGNED_OUT, out);
  }, []);

  if (state === "checking") return null;
  if (state === "out") return <SignIn onSignedIn={() => setState("in")} />;
  return <RouterProvider router={router} />;
}
