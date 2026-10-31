import { type FormEvent, useState } from "react";

import { api } from "../api/client";
import { Mark } from "../Shell";

/** The operator's sign-in: the password only, one account per install. */
export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { response } = await api.POST("/api/session", { body: { password } });
      if (response.ok) return onSignedIn();
      setPassword("");
      setError(
        response.status === 429
          ? "Too many wrong passwords. Try again in a few minutes."
          : "That is not the password. It is SWARMIFY_PASSWORD in the install's .env.",
      );
    } catch {
      setError("Could not reach the API. Is it running?");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="sign-in">
      <Mark />
      <form className="panel" onSubmit={submit} aria-label="Sign in">
        <h1>Sign in</h1>
        <p className="sub">This browser stays signed in for 30 days.</p>
        {error && <p role="alert">{error}</p>}
        <label>
          Password
          <input
            type="password"
            required
            autoFocus
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "Signing in..." : "Sign in"}
        </button>
      </form>
    </main>
  );
}
