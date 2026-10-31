import react from "@vitejs/plugin-react";
import { playwright } from "@vitest/browser-playwright";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
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
