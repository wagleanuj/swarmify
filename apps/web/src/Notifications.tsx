import { type ReactNode, useEffect, useState } from "react";

import { api } from "./api/client";
import { useConfirm } from "./Dialog";
import { type Device, disablePush, enablePush, type Here, snoozed, snoozePrompt, thisDevice } from "./push";
import { usePwa } from "./pwa";
import { Skeleton } from "./Skeleton";

type Kind = "gates" | "failures" | "budget" | "notices";
const KINDS: { kind: Kind; label: string; about: string }[] = [
  { kind: "gates", label: "Gates", about: "A run waits on your answer." },
  { kind: "failures", label: "Failed runs", about: "A run stopped with an error." },
  { kind: "budget", label: "Budget", about: "80 percent spent, or paused at 100." },
  { kind: "notices", label: "Needs-you notices", about: "A sign-in expired, the network is down, a post stayed hidden." },
];

const STATE: Record<Exclude<Here["state"], "on">, string> = {
  off: "Off on this device.",
  blocked: "Blocked by the browser. Allow notifications for this site in the browser's site settings, then reload.",
  "ios-install":
    "On iPhone and iPad, notifications work only in the installed app. In Safari, tap Share, then Add to Home Screen, and open Swarmify from there.",
  unsupported: "This browser cannot get notifications from Swarmify. Open it over https in Chrome, Edge, Firefox or Safari.",
};

const when = (iso: string | null) => (iso ? `last delivered ${new Date(iso).toLocaleDateString()}` : "nothing delivered yet");

/** Settings' Notifications section: this device on or off, a test, its kinds, the other devices. */
export function NotificationsRow({ Row }: { Row: (props: { title: string; about: string; children: ReactNode }) => ReactNode }) {
  const [confirm, dialog] = useConfirm();
  const [here, setHere] = useState<Here | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ text: string; bad?: boolean } | null>(null);

  const load = () =>
    api
      .GET("/api/push/subscriptions")
      .then(({ data }) => setDevices(data ?? []))
      .catch(() => undefined);
  useEffect(() => {
    thisDevice()
      .then((h) => {
        setHere(h);
        return load();
      })
      .catch(() => setHere({ state: "off", device: null }));
  }, []);

  async function run(step: () => Promise<void>) {
    setBusy(true);
    setSaid(null);
    try {
      await step();
    } catch (e) {
      setSaid({ text: e instanceof Error ? e.message : "That did not work. Try again.", bad: true });
    }
    setBusy(false);
  }

  const turnOn = () =>
    run(async () => {
      const next = await enablePush();
      setHere(next);
      if (next.state === "on") setSaid({ text: "On. Send a test to see one arrive." });
      await load();
    });
  const turnOff = () =>
    run(async () => {
      await disablePush(here?.device ?? null);
      setHere({ state: "off", device: null });
      setSaid({ text: "Off. This device gets nothing now." });
      await load();
    });
  const test = (device: Device) =>
    run(async () => {
      const { response, error } = await api.POST("/api/push/test/{device_id}", { params: { path: { device_id: device.id } } });
      if (!response.ok) throw new Error((error as { detail?: string } | undefined)?.detail ?? "The test did not go out.");
      setSaid({ text: "Sent. It should arrive within a few seconds." });
    });
  const toggle = (device: Device, kind: Kind, on: boolean) =>
    run(async () => {
      const { data } = await api.PATCH("/api/push/subscriptions/{device_id}", { params: { path: { device_id: device.id } }, body: { [kind]: on } });
      if (!data) throw new Error("Not saved. Try again.");
      setHere((h) => (h ? { ...h, device: data } : h));
      setSaid({ text: "Saved." });
    });
  async function remove(device: Device) {
    const lines = ["It gets no more notifications until it is turned on there again."];
    if (!(await confirm({ title: `Remove ${device.label}?`, lines, action: "Remove", danger: true }))) return;
    await run(async () => {
      const { response } = await api.DELETE("/api/push/subscriptions/{device_id}", { params: { path: { device_id: device.id } } });
      if (!response.ok && response.status !== 404) throw new Error("Could not remove it. Try again.");
      setDevices((d) => d.filter((x) => x.id !== device.id));
    });
  }

  const mine = here?.device ?? null;
  const others = devices.filter((d) => d.id !== mine?.id);
  return (
    <>
      {dialog}
      <Row title="Notifications" about="This device buzzes when a run needs you, and a tap opens that gate. Set per device, for every world.">
        <div className="panel set-panel push-panel" aria-label="Notifications">
          <p data-testid="push-state">
            {here === null ? <Skeleton w="14em" h="1.5em" /> : here.state === "on" ? `On for this device${mine ? `, ${mine.label}` : ""}.` : STATE[here.state]}
          </p>
          {(here?.state === "on" || here?.state === "off") && (
            <figure className="push-preview" aria-label="What a notification looks like">
              <img src="/icon-192.png" alt="" width={32} height={32} />
              <figcaption>
                <b>Your workflow needs you</b>
                <span>The gate's question, as the workflow asks it.</span>
              </figcaption>
            </figure>
          )}
          {here?.state === "on" && mine && (
            <fieldset className="push-kinds" disabled={busy}>
              <legend className="push-legend">Tell this device when</legend>
              {KINDS.map(({ kind, label, about }) => (
                <label key={kind} className="check">
                  <input type="checkbox" checked={mine[kind]} onChange={(e) => void toggle(mine, kind, e.target.checked)} />
                  <span>
                    {label} <small className="faint">{about}</small>
                  </span>
                </label>
              ))}
            </fieldset>
          )}
          <div className="set-save start">
            {here?.state === "off" && (
              <button type="button" className="primary" onClick={() => void turnOn()} disabled={busy}>
                Turn on for this device
              </button>
            )}
            {here?.state === "on" && (
              <>
                {mine && (
                  <button type="button" className="primary" onClick={() => void test(mine)} disabled={busy}>
                    Send a test
                  </button>
                )}
                <button type="button" onClick={() => void turnOff()} disabled={busy}>
                  Turn off
                </button>
              </>
            )}
            {said && (said.bad ? <p role="alert">{said.text}</p> : <p className="ok" role="status">{said.text}</p>)}
          </div>
          {others.length > 0 && <p className="push-legend">Other devices</p>}
          {others.length > 0 && (
            <ul className="token-list" aria-label="Other devices">
              {others.map((d) => (
                <li key={d.id}>
                  <span>
                    {d.label}
                    <small className="faint">{when(d.last_success_at)}</small>
                  </span>
                  <button type="button" className="danger" aria-label={`Remove ${d.label}`} onClick={() => void remove(d)} disabled={busy}>
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Row>
    </>
  );
}

/** The one-time ask, right after a gate was answered on this device: never on load, never while
 * the install hint is up, and away for 30 days after Not now. The browser's own permission
 * prompt only follows Turn on. */
export function PushPrompt({ hidden }: { hidden: boolean }) {
  const { answeredNow } = usePwa();
  const [state, setState] = useState<Here["state"] | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [closed, setClosed] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (answeredNow && !snoozed()) void thisDevice().then((h) => setState(h.state), () => undefined);
  }, [answeredNow]);
  useEffect(() => {
    if (!done) return;
    const t = setTimeout(() => setClosed(true), 5000);
    return () => clearTimeout(t);
  }, [done]);

  if (closed || hidden || !answeredNow) return null;
  if (done)
    return (
      <p className="pwa-note" role="status">
        {done}
      </p>
    );
  if (state !== "off" && state !== "ios-install") return null;
  const later = () => {
    snoozePrompt();
    setClosed(true);
  };
  const turnOn = () => {
    setBusy(true);
    enablePush()
      .then((h) =>
        setDone(h.state === "on" ? "Notifications are on for this device." : h.state === "blocked" ? "The browser blocked them. Settings, Notifications says how to allow them." : "Not turned on. Settings, Notifications has it any time."),
      )
      .catch((e: unknown) => setDone(e instanceof Error ? e.message : "That did not work. Try it in Settings, Notifications."))
      .finally(() => {
        snoozePrompt();
        setBusy(false);
      });
  };
  return (
    <aside className="pwa-hint" aria-label="Turn on notifications">
      <p>
        <strong>Get a nudge when a run needs you next?</strong>
        <span className="sub">
          {state === "ios-install"
            ? "On iPhone, first add Swarmify to the Home Screen: in Safari tap Share, then Add to Home Screen. Turn it on in the app's Settings."
            : "Only when a gate opens or something needs fixing. Change it any time in Settings."}
        </span>
      </p>
      <div className="pwa-hint-actions">
        <button type="button" onClick={later} disabled={busy}>
          Not now
        </button>
        {state === "off" && (
          <button type="button" className="primary" onClick={turnOn} disabled={busy}>
            Turn on
          </button>
        )}
      </div>
    </aside>
  );
}
