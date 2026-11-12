// The app icons as PNGs, drawn from the SVG sources beside this file by Playwright's Chromium.
// Run after changing a source: `pnpm --filter web icons`. The PNGs are committed in public/.
import { readFile, writeFile, copyFile } from "node:fs/promises";
import { chromium } from "playwright";

const here = new URL(".", import.meta.url);
const pub = new URL("../public/", import.meta.url);
const PNGS = [
  ["icon.svg", "icon-192.png", 192],
  ["icon.svg", "icon-512.png", 512],
  ["icon-maskable.svg", "icon-maskable-512.png", 512],
  // iOS rounds the corners itself, so it gets the full-bleed square.
  ["icon-maskable.svg", "apple-touch-icon.png", 180],
  ["favicon.svg", "favicon-32.png", 32],
  // Android notification badge: only the alpha is drawn.
  ["icon-mono.svg", "badge-96.png", 96],
];

const browser = await chromium.launch();
const page = await browser.newPage();
for (const [source, out, size] of PNGS) {
  const svg = await readFile(new URL(source, here), "utf8");
  await page.setViewportSize({ width: size, height: size });
  await page.setContent(
    `<style>html,body{margin:0;background:transparent}svg{display:block;width:${size}px;height:${size}px}</style>${svg}`,
  );
  await writeFile(new URL(out, pub), await page.screenshot({ omitBackground: true }));
}
await browser.close();
await copyFile(new URL("favicon.svg", here), new URL("favicon.svg", pub));
console.log(`wrote ${PNGS.length} PNGs and favicon.svg to public/`);
