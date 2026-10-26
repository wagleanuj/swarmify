import { RouterProvider } from "@tanstack/react-router";

import { createAppRouter } from "./router";

const router = createAppRouter();

export function App() {
  return <RouterProvider router={router} />;
}
