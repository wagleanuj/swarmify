"""Swarmify installs as an app (issue 458): a valid manifest with reachable icons, a service
worker that controls the page, and a cache that never holds an API response or event stream."""

import httpx
from playwright.sync_api import Browser, Page, expect


def test_manifest_and_icons_make_the_app_installable(base_url: str) -> None:
    manifest_response = httpx.get(f"{base_url}/manifest.webmanifest")
    assert manifest_response.headers["content-type"] == "application/manifest+json"
    manifest = manifest_response.json()
    assert manifest["name"] == manifest["short_name"] == "Swarmify"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/gates" and manifest["scope"] == "/"
    assert manifest["theme_color"] == manifest["background_color"] == "#171719"
    sizes = {(icon["sizes"], icon.get("purpose", "any")) for icon in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    icons = [icon["src"] for icon in manifest["icons"]]
    for src in [*icons, "/apple-touch-icon.png", "/favicon.svg", "/favicon-32.png"]:
        icon = httpx.get(f"{base_url}{src}")
        assert icon.status_code == 200, src
        assert icon.headers["content-type"].startswith("image/"), src
    sw = httpx.get(f"{base_url}/sw.js")
    assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/"


def test_service_worker_controls_the_app_and_skips_the_api(page: Page, base_url: str) -> None:
    page.goto(base_url)
    expect(page.get_by_role("heading", name="Your worlds")).to_be_visible()
    head = page.locator("head")
    expect(head.locator('link[rel="manifest"]')).to_have_attribute("href", "/manifest.webmanifest")
    expect(head.locator('meta[name="theme-color"]')).to_have_attribute("content", "#171719")
    page.wait_for_function("navigator.serviceWorker.ready.then(() => true)")
    # The first visit installs it; the next load is the one it controls.
    page.reload()
    expect(page.get_by_role("heading", name="Your worlds")).to_be_visible()
    page.wait_for_function("navigator.serviceWorker.controller !== null")
    page.goto(f"{base_url}/gates")
    page.wait_for_load_state("networkidle")

    cached: list[str] = page.evaluate(
        """async () => {
          const urls = [];
          for (const key of await caches.keys()) {
            const cache = await caches.open(key);
            urls.push(...(await cache.keys()).map((r) => new URL(r.url).pathname));
          }
          return urls;
        }"""
    )
    assert "/index.html" in cached
    assert any(url.startswith("/assets/") for url in cached)
    assert not [url for url in cached if url.startswith("/api/") or url == "/mcp"]


def test_on_a_phone_it_feels_like_an_app(browser: Browser, base_url: str) -> None:
    """No pinch or double-tap zoom, no zoom into a field, no grey tap flash, no rubber band."""
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, base_url=base_url
    )
    page = context.new_page()
    page.goto("/")
    expect(page.get_by_role("heading", name="Your worlds")).to_be_visible()
    viewport = page.locator('meta[name="viewport"]').get_attribute("content") or ""
    parts = {p.strip() for p in viewport.split(",")}
    want = {"user-scalable=no", "maximum-scale=1", "width=device-width", "viewport-fit=cover"}
    assert want <= parts
    feel = page.evaluate(
        """() => {
          const html = getComputedStyle(document.documentElement);
          const typed = "input:not([type=checkbox]):not([type=radio]), select, textarea";
          const fields = [...document.querySelectorAll(typed)];
          return {
            tap: html.webkitTapHighlightColor,
            touch: html.touchAction,
            overscroll: [
              html.overscrollBehaviorY, getComputedStyle(document.body).overscrollBehaviorY,
            ],
            fields: fields.map((f) => parseFloat(getComputedStyle(f).fontSize)),
            buttonSelect: getComputedStyle(document.querySelector("button")).userSelect,
          };
        }"""
    )
    assert feel["tap"] in ("rgba(0, 0, 0, 0)", "transparent")
    assert feel["touch"] == "manipulation"
    assert feel["overscroll"] == ["none", "none"]
    assert feel["fields"] and min(feel["fields"]) >= 16
    assert feel["buttonSelect"] == "none"
    context.close()
