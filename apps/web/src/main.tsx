import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./index.css";
import { PwaNotices } from "./PwaNotices";
import { stopPinch } from "./pinch";
import { startPwa } from "./pwa";
import { startResync } from "./refresh";
import { applyTextSize } from "./textSize";

startPwa();
startResync();
stopPinch();
applyTextSize();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
    <PwaNotices />
  </StrictMode>,
);
