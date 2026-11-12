import { type FormEvent, useState } from "react";

import { api } from "../api/client";
import { Icon } from "../Icon";
import { Mark } from "../Shell";

/** The operator's sign-in: the password only, one account per install. The mark arrives, the
 * form sits centred in the safe area, and a refused password shakes the form once. */
export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [password, setPassword] = useState("");
  const [shown, setShown] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Bumped on every refusal, so the form's key changes and its shake plays again.
  const [refusals, setRefusals] = useState(0);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { response } = await api.POST("/api/session", { body: { password } });
      if (response.ok) return onSignedIn();
      setPassword("");
      setRefusals((n) => n + 1);
      setError(
        response.status === 429
          ? "Too many wrong passwords. Wait a few minutes, then try again."
          : "That is not the password. Use SWARMIFY_PASSWORD from the install's .env.",
      );
    } catch {
      setRefusals((n) => n + 1);
      setError("Could not reach the API. Start it, then try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="sign-in">
      <div className="mark-in">
        <Mark />
      </div>
      <form key={refusals} className={`panel${refusals ? " shake" : ""}`} onSubmit={submit} aria-label="Sign in">
        <h1>Sign in</h1>
        <p className="sub">This browser stays signed in for 30 days.</p>
        {error && <p role="alert">{error}</p>}
        <label htmlFor="password">Password</label>
        <div className="password">
          <input
            id="password"
            name="password"
            type={shown ? "text" : "password"}
            required
            autoFocus
            autoComplete="current-password"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button
            type="button"
            className="icon"
            aria-label={shown ? "Hide password" : "Show password"}
            aria-pressed={shown}
            onClick={() => setShown((s) => !s)}
          >
            <Icon name={shown ? "eye-off" : "eye"} size={20} />
          </button>
        </div>
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "Signing in..." : "Sign in"}
        </button>
      </form>
    </main>
  );
}
