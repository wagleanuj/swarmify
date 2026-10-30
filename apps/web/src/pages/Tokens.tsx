import { type FormEvent, type ReactNode, useEffect, useState } from "react";

import { api, SIGNED_OUT } from "../api/client";
import type { components } from "../api/schema";

type Token = components["schemas"]["Token"];
type Said = { text: string; bad?: boolean };

/** The install's sign-in and API tokens, under every world's settings: they are not per world. */
export function Tokens({ Row }: { Row: (p: { title: string; about: string; children: ReactNode }) => ReactNode }) {
  const [tokens, setTokens] = useState<Token[] | null>(null);
  const [required, setRequired] = useState(false);
  const [name, setName] = useState("");
  const [made, setMade] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<Said | null>(null);

  useEffect(() => {
    api
      .GET("/api/tokens")
      .then(({ data }) => setTokens(data ?? []))
      .catch(() => setTokens([]));
    api
      .GET("/api/session")
      .then(({ data }) => setRequired(data?.sign_in_required ?? false))
      .catch(() => undefined);
  }, []);

  async function create(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setSaid(null);
    const { data } = await api.POST("/api/tokens", { body: { name } });
    setBusy(false);
    if (!data) {
      setSaid({ text: "Not created. Give it a name and try again.", bad: true });
      return;
    }
    const { token, ...shown } = data;
    setTokens((t) => [...(t ?? []), shown]);
    setMade(token);
    setName("");
  }

  async function revoke(token: Token) {
    if (!window.confirm(`Revoke ${token.name}? Anything using it is refused from now on.`)) return;
    setBusy(true);
    setSaid(null);
    const { response } = await api.DELETE("/api/tokens/{token_id}", { params: { path: { token_id: token.id } } });
    setBusy(false);
    if (!response.ok) {
      setSaid({ text: "Could not revoke it. Try again.", bad: true });
      return;
    }
    setTokens((t) => (t ?? []).filter((x) => x.id !== token.id));
    setMade(null);
    setSaid({ text: `Revoked ${token.name}.` });
  }

  async function signOut() {
    await api.DELETE("/api/session").catch(() => undefined);
    window.dispatchEvent(new Event(SIGNED_OUT));
  }

  return (
    <>
      <Row
        title="API tokens"
        about="For the whole install, not only this world. The MCP server at /mcp takes a token instead of your password, so tools never hold it."
      >
        <form className="panel set-panel" onSubmit={create} aria-label="API tokens">
          {made && (
            <div className="token-new" role="status">
              <span className="ok">Copy it now. It is not shown again.</span>
              <pre className="mono" data-testid="new-token">
                {made}
              </pre>
              <span className="faint">Send it as Authorization: Bearer, as the README's Sign-in section shows for Claude Code.</span>
            </div>
          )}
          {tokens === null ? (
            <p className="faint">Loading...</p>
          ) : tokens.length === 0 ? (
            <p className="faint">No tokens yet.</p>
          ) : (
            <ul className="token-list" aria-label="Tokens">
              {tokens.map((t) => (
                <li key={t.id}>
                  <span>
                    {t.name}
                    <small className="faint">created {new Date(t.created_at).toLocaleDateString()}</small>
                  </span>
                  <button type="button" className="danger" aria-label={`Revoke ${t.name}`} onClick={() => void revoke(t)} disabled={busy}>
                    Revoke
                  </button>
                </li>
              ))}
            </ul>
          )}
          <label>
            New token's name
            <input required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} placeholder="Claude Code on the laptop" autoComplete="off" />
          </label>
          <div className="set-save">
            {said && (said.bad ? <p role="alert">{said.text}</p> : <p className="ok">{said.text}</p>)}
            <button type="submit" disabled={busy}>
              Create token
            </button>
          </div>
        </form>
      </Row>
      {required && (
        <Row title="Sign-in" about="One operator password for the install: SWARMIFY_PASSWORD in its .env.">
          <div className="panel set-panel">
            <p>This browser stays signed in for 30 days unless you sign out.</p>
            <div className="set-save start">
              <button type="button" onClick={() => void signOut()}>
                Sign out
              </button>
            </div>
          </div>
        </Row>
      )}
    </>
  );
}
