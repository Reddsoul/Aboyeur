import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import './Profile.css';
import './Clients.css';
import * as api from '../api';
import { ApiError } from '../api';
import { useAuth } from '../auth/AuthContext';
import { useClients } from '../clients/ClientContext';

/** The account screen: who is logged in, not who they're planning for.
 *
 * Deliberately separate from the client workspace — the practitioner's own
 * meals live on their "Me" client record like anyone else's, and this page
 * only links there. Mixing the two is what made the avatar button confusing
 * in the first place. */
export default function Account() {
  const navigate = useNavigate();
  const { logout } = useAuth();
  const { setActiveClient, refreshClients } = useClients();

  const [account, setAccount] = useState<api.Account | null>(null);
  const [form, setForm] = useState({ name: '', practice: '', email: '' });
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<{ kind: 'ok' | 'bad'; text: string } | null>(null);

  const [pw, setPw] = useState({ current: '', next: '', confirm: '' });
  const [pwBusy, setPwBusy] = useState(false);
  const [pwStatus, setPwStatus] = useState<{ kind: 'ok' | 'bad'; text: string } | null>(null);

  useEffect(() => {
    api
      .getAccount()
      .then((a) => {
        setAccount(a);
        setForm({ name: a.name ?? '', practice: a.practice ?? '', email: a.email });
      })
      .catch(() => setStatus({ kind: 'bad', text: "Couldn't load your account." }));
  }, []);

  const patch = (p: Partial<typeof form>) => {
    setForm((f) => ({ ...f, ...p }));
    setDirty(true);
    setStatus(null);
  };

  async function save() {
    setSaving(true);
    setStatus(null);
    try {
      const saved = await api.updateAccount({
        name: form.name,
        practice: form.practice,
        email: form.email,
      });
      setAccount(saved);
      setDirty(false);
      setStatus({ kind: 'ok', text: 'Saved.' });
    } catch (err) {
      const detail =
        err instanceof ApiError && err.status === 409
          ? 'Another account already uses that email.'
          : err instanceof ApiError
            ? `Server said ${err.status}.`
            : 'Check your connection.';
      setStatus({ kind: 'bad', text: `Couldn't save. ${detail}` });
    } finally {
      setSaving(false);
    }
  }

  async function savePassword() {
    if (pw.next !== pw.confirm) {
      setPwStatus({ kind: 'bad', text: "The new passwords don't match." });
      return;
    }
    setPwBusy(true);
    setPwStatus(null);
    try {
      await api.changePassword({ current_password: pw.current, new_password: pw.next });
      setPw({ current: '', next: '', confirm: '' });
      setPwStatus({ kind: 'ok', text: 'Password changed. Other devices stay signed in.' });
    } catch (err) {
      const detail =
        err instanceof ApiError && err.status === 403
          ? 'Current password is wrong.'
          : err instanceof ApiError && err.status === 400
            ? 'Use at least 8 characters.'
            : 'Try again.';
      setPwStatus({ kind: 'bad', text: detail });
    } finally {
      setPwBusy(false);
    }
  }

  if (!account) {
    return (
      <div className="pf-page">
        <div className="pf-inner">
          <p className="pf-sub">{status?.text ?? 'Loading your account…'}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="pf-page">
      <div className="pf-inner">
        <div className="pf-head">
          <div>
            <h1 className="pf-title">Your account</h1>
            <p className="pf-sub">
              You, the practitioner. {account.active_client_count} active client
              {account.active_client_count === 1 ? '' : 's'} on your book.
            </p>
          </div>
          <Link className="pf-link-btn" to="/onboarding?tour=1">
            Replay the tour
          </Link>
        </div>

        <section className="pf-section">
          <div>
            <h2 className="pf-section-title">Details</h2>
            <p className="pf-section-sub">Your name and practice. Never sent to the model.</p>
          </div>
          <div className="pf-grid">
            <div className="pf-field">
              <label className="pf-label">Your name</label>
              <input className="text-input" value={form.name} onChange={(e) => patch({ name: e.target.value })} placeholder="Jose Alma" />
            </div>
            <div className="pf-field">
              <label className="pf-label">Practice</label>
              <input className="text-input" value={form.practice} onChange={(e) => patch({ practice: e.target.value })} placeholder="Optional" />
            </div>
            <div className="pf-field">
              <label className="pf-label">Sign-in email</label>
              <input className="text-input" type="email" value={form.email} onChange={(e) => patch({ email: e.target.value })} />
            </div>
          </div>
          <div className="pf-savebar" style={{ position: 'static' }}>
            <button type="button" className="btn-primary" disabled={!dirty || saving} onClick={() => void save()}>
              {saving ? 'Saving…' : 'Save account'}
            </button>
            <span className={`pf-status ${status?.kind === 'ok' ? 'pf-status-ok' : status?.kind === 'bad' ? 'pf-status-bad' : ''}`}>
              {status?.text ?? (dirty ? 'Unsaved changes.' : 'Everything saved.')}
            </span>
          </div>
        </section>

        <section className="pf-section">
          <div>
            <h2 className="pf-section-title">Your own meals</h2>
            <p className="pf-section-sub">
              You're a client too. Your household, targets and plans live on a client record like
              everyone else's — same isolation, nothing shared with the people you coach.
            </p>
          </div>
          {account.self_client ? (
            <div className="cl-plan">
              <div className="cl-avatar" style={{ width: 34, height: 34, fontSize: 13 }}>
                {account.self_client.name.slice(0, 2).toUpperCase()}
              </div>
              <div className="cl-plan-main">
                <strong>{account.self_client.name}</strong>
                <span>Your own client record</span>
              </div>
              <button
                type="button"
                className="btn-ghost"
                onClick={async () => {
                  const list = await refreshClients();
                  const mine = list.find((c) => c.id === account.self_client?.id);
                  if (mine) setActiveClient(mine);
                  navigate(`/app/clients/${account.self_client?.id}`);
                }}
              >
                Open
              </button>
            </div>
          ) : (
            <p className="pf-sub">No client record yet — reload to have one created.</p>
          )}
        </section>

        <section className="pf-section">
          <div>
            <h2 className="pf-section-title">Password</h2>
            <p className="pf-section-sub">Changing it doesn't sign out your other devices.</p>
          </div>
          <div className="pf-grid">
            <div className="pf-field">
              <label className="pf-label">Current password</label>
              <input className="text-input" type="password" autoComplete="current-password" value={pw.current} onChange={(e) => setPw((p) => ({ ...p, current: e.target.value }))} />
            </div>
            <div className="pf-field">
              <label className="pf-label">New password</label>
              <input className="text-input" type="password" autoComplete="new-password" value={pw.next} onChange={(e) => setPw((p) => ({ ...p, next: e.target.value }))} />
            </div>
            <div className="pf-field">
              <label className="pf-label">Confirm new password</label>
              <input className="text-input" type="password" autoComplete="new-password" value={pw.confirm} onChange={(e) => setPw((p) => ({ ...p, confirm: e.target.value }))} />
            </div>
          </div>
          <div className="pf-savebar" style={{ position: 'static' }}>
            <button type="button" className="btn-primary" disabled={!pw.next || !pw.confirm || pwBusy} onClick={() => void savePassword()}>
              {pwBusy ? 'Changing…' : 'Change password'}
            </button>
            {pwStatus && (
              <span className={`pf-status ${pwStatus.kind === 'ok' ? 'pf-status-ok' : 'pf-status-bad'}`}>{pwStatus.text}</span>
            )}
          </div>
        </section>

        <div className="cl-danger-zone">
          <span>Signed in as {account.email}.</span>
          <button
            type="button"
            className="btn-ghost"
            onClick={async () => {
              await logout();
              navigate('/login', { replace: true });
            }}
          >
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}
