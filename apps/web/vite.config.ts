import react from "@vitejs/plugin-react";
import { playwright } from "@vitest/browser-playwright";
import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

/** /sw.js from sw.js, with the build's own files as the app shell it precaches (issue 458). */
const serviceWorker = (): Plugin => ({
  name: "swarmify-sw",
  apply: "build",
  generateBundle(_, bundle) {
    const built = Object.keys(bundle).filter((f) => !f.endsWith(".map"));
    // index.html is emitted after this hook runs, so it is named here.
    const shell = [...new Set(["index.html", ...built, ...readdirSync("public")])].map((f) => `/${f}`);
    const hash = createHash("sha256");
    for (const file of Object.values(bundle)) hash.update(file.type === "chunk" ? file.code : file.source);
    const source = readFileSync("sw.js", "utf8")
      .replace("__PRECACHE__", JSON.stringify(shell))
      .replace("__VERSION__", JSON.stringify(hash.digest("hex").slice(0, 12)));
    this.emitFile({ type: "asset", fileName: "sw.js", source });
  },
});

export default defineConfig({
  plugins: [react(), serviceWorker()],
  server: { proxy: { "/api": "http://localhost:8000" } },
  test: {
    include: ["src/**/*.test.tsx"],
    browser: {
      enabled: true,
      headless: true,
      // A desktop window by default; phone tests set 390 x 844 themselves.
      viewport: { width: 1280, height: 800 },
      // The office's motion is cosmetic; with reduced motion its scene is static to test.
      provider: playwright({ contextOptions: { reducedMotion: "reduce" } }),
      instances: [{ browser: "chromium" }],
    },
  },
});
