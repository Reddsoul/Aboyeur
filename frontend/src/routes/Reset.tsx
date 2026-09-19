import { useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import './Auth.css';
import * as api from '../api';
import Logo from '../components/Logo';

/** Two screens in one route: /reset asks for an email; /reset?token=… sets a
 * new password. The request form always says "sent" — the server does the
 * same, so the form can't be used to check whether an email exists. */
export default function Reset() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get('token');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function request(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.requestReset(email);
      setDone(true);
    } catch (err) {
      setError(err instanceof api.ApiError && err.status === 429 ? 'Too many requests — try again in an hour.' : "Couldn't reach Aboyeur.");
    } finally {
      setBusy(false);
    }
  }

  async function confirmReset(e: FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError("The passwords don't match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.confirmReset(token!, password);
      setDone(true);
      setTimeout(() => navigate('/login', { replace: true }), 1500);
    } catch (err) {
      setError(err instanceof api.ApiError ? err.message : "Couldn't reach Aboyeur.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="au-stage">
      <div className="au-orb" />
      <form className="au-card" onSubmit={token ? confirmReset : request}>
        <Logo size={56} className="login-mark" />
        <div style={{ textAlign: 'center' }}>
          <h1 className="au-title">{token ? 'Choose a new password' : 'Reset your password'}</h1>
          <p className="au-sub">
            {token ? 'The link works once and expires after an hour.' : "Enter your email and we'll send a one-hour link."}
          </p>
        </div>

        {token ? (
          <>
            <div>
              <label className="au-label">New password</label>
              <input className="text-input" type="password" required minLength={10} autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="At least 10 characters" />
            </div>
            <div>
              <label className="au-label">Confirm</label>
              <input className="text-input" type="password" required autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            </div>
          </>
        ) : (
          <div>
            <label className="au-label">Email</label>
            <input className="text-input" type="email" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
          </div>
        )}

        {error && <div className="au-error">{error}</div>}
        {done && (
          <div className="au-ok">
            {token ? 'Password changed — taking you to sign in.' : 'If that address has an account, a reset link is on its way.'}
          </div>
        )}

        <button type="submit" className="btn-primary" disabled={busy || done}>
          {busy ? 'Working…' : token ? 'Set password' : 'Send reset link'}
        </button>
        <p className="au-fine">
          <Link to="/login">Back to sign in</Link>
        </p>
      </form>
    </div>
  );
}
