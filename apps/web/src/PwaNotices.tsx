import { type ReactNode, useState } from "react";

import { PushPrompt } from "./Notifications";
import { dismissInstall, earnedInstallHint, install, ios, standalone, update, usePwa } from "./pwa";
import "./styles/pwa.css";
import { usePhone } from "./usePhone";

/** The app's own notices, over every view: offline, a new version ready, once on a phone the
 * offer to install, and after a gate is answered the offer of notifications. Mounted beside the app in main.tsx. */
export function PwaNotices() {
  const { waiting, online, offer, office } = usePwa();
  const phone = usePhone();
  const [later, setLater] = useState(false);
  // Decided once per page load, so the prompt does not pop in while the person reads.
  const [earned] = useState(earnedInstallHint);
  const [hinted, setHinted] = useState(false);
  const hint = phone && !office && earned && !hinted && !standalone() && (offer !== null || ios());
  const done = () => {
    dismissInstall();
    setHinted(true);
  };

  return (
    <>
      <div className={office ? "pwa-notes on-office" : "pwa-notes"}>
        {!online && (
          <p className="pwa-note" role="status">
            Offline. Live runs and gates come back when you reconnect.
          </p>
        )}
        {waiting && !later && (
          <div className="pwa-note" role="status">
            <span>A new version of Swarmify is ready.</span>
            <button type="button" className="link" onClick={() => setLater(true)}>
              Later
            </button>
            <button type="button" className="primary" onClick={update}>
              Reload
            </button>
          </div>
        )}
        {hint && (
        <aside className="pwa-hint" aria-label="Install Swarmify">
          <p>
            <strong>Put Needs you on your home screen</strong>
            <span className="sub">{offer ? "Your gates open in one tap, full screen." : "In Safari, tap Share, then Add to Home Screen."}</span>
          </p>
          <div className="pwa-hint-actions">
            <button type="button" onClick={done}>
              {offer ? "No thanks" : "Got it"}
            </button>
            {offer && (
              <button type="button" className="primary" onClick={() => install().finally(done)}>
                Install
              </button>
            )}
          </div>
        </aside>
        )}
        <PushPrompt hidden={hint} />
      </div>
    </>
  );
}

/** Settings' row for installing on this device, always there after the phone prompt is gone. */
export function InstallRow({ Row }: { Row: (props: { title: string; about: string; children: ReactNode }) => ReactNode }) {
  const { offer } = usePwa();
  return (
    <Row title="This device" about="Install Swarmify to open it from the home screen, full screen, at Needs you.">
      <div className="panel set-panel">
        {standalone() ? (
          <p>Installed. You are using the Swarmify app.</p>
        ) : offer ? (
          <div className="set-save start">
            <button type="button" className="primary" onClick={() => install()}>
              Install Swarmify
            </button>
          </div>
        ) : ios() ? (
          <p>In Safari, tap Share, then Add to Home Screen.</p>
        ) : (
          <p className="sub">This browser has not offered an install. In Chrome or Edge, use Install in the address bar or the browser menu.</p>
        )}
      </div>
    </Row>
  );
}
