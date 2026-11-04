import { type FormEvent, useState } from "react";
import { supabase } from "../lib/supabase";

export function SignIn() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const { error: failed } = await supabase.auth.signInWithPassword({ email, password });
    if (failed) {
      setError(failed.status === 400
        ? "That email and password don't match. Check them and try again."
        : "Sign-in isn't available right now. Try again in a moment.");
      setBusy(false);
    }
  }

  return (
    <div className="signin">
      <section className="signin-field">
        <p className="wordmark">Invoice Auditor</p>
        <h1>Every invoice checked against the ERP. You make the call.</h1>
        <div className="verdict-bars" aria-hidden="true">
          <span className="bar-approve" />
          <span className="bar-review" />
          <span className="bar-reject" />
        </div>
      </section>
      <section className="signin-form">
        <form onSubmit={submit}>
          <h2>Sign in</h2>
          <label>
            Work email
            <input type="email" autoComplete="email" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <label>
            Password
            <input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </label>
          {error && <p className="error" role="alert">{error}</p>}
          <button className="button primary wide" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
          <p className="muted small">Accounts are created by your administrator.</p>
        </form>
      </section>
    </div>
  );
}
