/** Push notifications on this device (issue 459): whether it can have them, turning them on and
 * off, and when the quiet prompt may ask. The server side is /api/push; the service worker shows
 * what arrives (sw.js). */
import { api } from "./api/client";
import type { components } from "./api/schema";
import { ios, standalone } from "./pwa";

export type Device = components["schemas"]["Device"];
/** on and off are this browser's subscription; the rest say why it cannot have one now. */
export type PushState = "on" | "off" | "blocked" | "ios-install" | "unsupported";
export type Here = { state: PushState; device: Device | null };

const supported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const registration = async () => (supported() ? ((await navigator.serviceWorker.getRegistration()) ?? null) : null);

/** A name the device list can show: the browser on the system, from the user agent. */
export function deviceLabel(ua = navigator.userAgent): string {
  const system = /iphone/i.test(ua) ? "iPhone" : /ipad/i.test(ua) ? "iPad" : /android/i.test(ua) ? "Android" : /mac os/i.test(ua) ? "Mac" : /windows/i.test(ua) ? "Windows" : /linux/i.test(ua) ? "Linux" : "";
  const browser = /edg\//i.test(ua) ? "Edge" : /firefox|fxios/i.test(ua) ? "Firefox" : /chrome|crios/i.test(ua) ? "Chrome" : /safari/i.test(ua) ? "Safari" : "";
  return [browser, system].filter(Boolean).join(" on ") || "A browser";
}

async function store(sub: PushSubscription): Promise<Device> {
  const json = sub.toJSON();
  const keys = { p256dh: json.keys?.p256dh ?? "", auth: json.keys?.auth ?? "" };
  const { data } = await api.POST("/api/push/subscriptions", { body: { endpoint: sub.endpoint, keys, label: deviceLabel() } });
  if (!data) throw new Error("The server did not take this device. Try again.");
  return data;
}

/** This device's state; when it is subscribed, the server's record of it (stored again, so a
 * subscription the server dropped or whose keys changed comes back). */
export async function thisDevice(): Promise<Here> {
  if (ios() && !standalone()) return { state: "ios-install", device: null };
  if (!supported()) return { state: "unsupported", device: null };
  if (Notification.permission === "denied") return { state: "blocked", device: null };
  const sub = await (await registration())?.pushManager.getSubscription();
  if (!sub || Notification.permission !== "granted") return { state: "off", device: null };
  return { state: "on", device: await store(sub).catch(() => null) };
}

const key = (b64: string) => {
  const raw = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (b64.length % 4)) % 4));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
};

/** Ask the browser, then subscribe and store. Call it straight from a tap: browsers only ask
 * then. Throws with a sentence to show when something on the way fails. */
export async function enablePush(): Promise<Here> {
  const permission = await Notification.requestPermission();
  if (permission === "denied") return { state: "blocked", device: null };
  if (permission !== "granted") return { state: "off", device: null };
  const reg = await registration();
  if (!reg) throw new Error("The app's service worker is not running yet. Reload the page and try again.");
  const { data } = await api.GET("/api/push/key");
  if (!data) throw new Error("Could not reach the server. Try again.");
  const sub = (await reg.pushManager.getSubscription()) ?? (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key(data.public_key) }));
  return { state: "on", device: await store(sub) };
}

/** Unsubscribe this browser and forget it on the server. */
export async function disablePush(device: Device | null) {
  const sub = await (await registration())?.pushManager.getSubscription();
  await sub?.unsubscribe().catch(() => false);
  if (device) await api.DELETE("/api/push/subscriptions/{device_id}", { params: { path: { device_id: device.id } } });
}

const LATER = "swarmify.push.later";
const MONTH = 30 * 24 * 3600 * 1000;

/** "Not now" on the prompt: it stays away for 30 days. */
export function snoozePrompt(now = Date.now()) {
  try {
    localStorage.setItem(LATER, String(now));
  } catch {
    // Private mode: it may ask again next time.
  }
}

export function snoozed(now = Date.now()): boolean {
  try {
    return now - Number(localStorage.getItem(LATER) ?? 0) < MONTH;
  } catch {
    return false;
  }
}
