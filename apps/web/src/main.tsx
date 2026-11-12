import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./index.css";
import { PwaNotices } from "./PwaNotices";
import { startPwa } from "./pwa";

startPwa();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
    <PwaNotices />
  </StrictMode>,
);
