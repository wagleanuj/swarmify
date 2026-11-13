import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./index.css";
import { PwaNotices } from "./PwaNotices";
import { stopPinch } from "./pinch";
import { startPwa } from "./pwa";
import { applyTextSize } from "./textSize";

startPwa();
stopPinch();
applyTextSize();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
    <PwaNotices />
  </StrictMode>,
);
