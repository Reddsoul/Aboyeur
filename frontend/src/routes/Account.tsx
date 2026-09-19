import { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import './Profile.css';
import './Clients.css';
import './Auth.css';
import * as api from '../api';
import { ApiError } from '../api';
import { useAuth } from '../auth/AuthContext';
import { useClients } from '../clients/ClientContext';
import { ClinicalNote, LegalFooter } from '../components/Legal';
import { COUNTRIES, countryName } from '../countries';

const dollars = (cents: number | null | undefined) =>
  cents == null ? '—' : `$${(cents / 100).toFixed(cents % 100 === 0 ? 0 : 2)}`;

const STATUS_LABEL: Record<api.BillingStatus, string> = {
  free: 'Free',
  active: 'Pro',
  past_due: 'Pro — payment failed',
  canceled: 'Free (subscription ended)',
  comp: 'Pro — complimentary',
};

/** Plan, price, tenure, and the two Stripe-hosted doors (Checkout, Portal).
 * No card entry here, ever. */
function BillingSection() {
  const [params] = useSearchParams();
  const { refreshMe } = useAuth();
  const [me, setMe] = useState<api.BillingMe | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [status, setStatus] = useState<{ kind: 'ok' | 'bad'; text: string } | null>(null);
  const [country, setCountry] = useState('');

  const load = () =>
    api
      .billingMe()
      .then((b) => {
        setMe(b);
        setCountry(b.country_source === 'account' ? b.country : '');
      })
      .catch(() => setStatus({ kind: 'bad', text: "Couldn't load billing." }));

  useEffect(() => {
    void load();
    const checkout = params.get('checkout');
    if (checkout === 'success') {
      setStatus({ kind: 'ok', text: 'Payment received — your plan updates within a few seconds.' });
      // Webhooks land a beat after the redirect; poll briefly so the page
      // flips to Pro without a manual reload.
      const t = setTimeout(() => {
        void load();
        void refreshMe();
      }, 4000);
      return () => clearTimeout(t);
    }
    if (checkout === 'cancel') setStatus({ kind: 'bad', text: 'Checkout cancelled — nothing was charged.' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function go(kind: string, fn: () => Promise<{ url: string }>) {
    setBusy(kind);
    setStatus(null);
    try {
      const { url } = await fn();
      window.location.assign(url);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? err.code === 'unverified'
            ? 'Verify your email first (see the banner above).'
            : err.code === 'billing_disabled'
              ? "Billing isn't enabled on this deploy yet."
              : err.message
          : "Couldn't reach Aboyeur.";
      setStatus({ kind: 'bad', text: msg });
      setBusy(null);
    }
  }

  async function saveCountry(code: string) {
    setCountry(code);
    try {
      await api.setCountry(code || null);
      await load();
    } catch {
      setStatus({ kind: 'bad', text: "Couldn't save your country." });
    }
  }

  if (!me) return <p className="pf-sub">{status?.text ?? 'Loading billing…'}</p>;

  const paid = me.billing_status === 'active' || me.billing_status === 'past_due';
  const nextTier = me.ladder.find((t) => t.min_paid_months > me.paid_months);

  return (
    <>
      <div className="cl-plan" style={{ alignItems: 'flex-start' }}>
        <div className="cl-plan-main" style={{ gap: 4 }}>
          <strong style={{ fontSize: 16 }}>{STATUS_LABEL[me.billing_status]}</strong>
          {paid && (
            <span>
              {me.interval === 'year' ? 'Yearly' : 'Monthly'}
              {me.renews_at && ` · renews ${new Date(me.renews_at).toLocaleDateString()}`}
              {me.percent_off > 0 && ` · ${me.percent_off}% loyalty discount`}
            </span>
          )}
          {me.billing_status === 'past_due' && (
            <span style={{ color: 'var(--danger)' }}>
              Your last payment failed. Update your card in the billing portal to keep Pro.
            </span>
          )}
          {me.billing_status === 'comp' && <span>On the house. Hosted models are unmetered for you.</span>}
          {!paid && me.billing_status !== 'comp' && (
            <span>
              Bring your own API key for unlimited use on Free. Pro adds Aboyeur's hosted models
              (pay-per-use credit) and priority support.
            </span>
          )}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'flex-end' }}>
          {!paid && me.billing_status !== 'comp' && me.billing_enabled && (
            <>
              <button type="button" className="btn-primary" disabled={busy !== null} onClick={() => void go('month', () => api.billingCheckout('month'))}>
                {busy === 'month' ? 'Opening…' : `Go Pro — ${dollars(me.prices.month)}/mo`}
              </button>
              <button type="button" className="btn-ghost" disabled={busy !== null} onClick={() => void go('year', () => api.billingCheckout('year'))}>
                {busy === 'year' ? 'Opening…' : `${dollars(me.prices.year)}/yr`}
              </button>
            </>
          )}
          {me.has_customer && me.billing_enabled && (
            <button type="button" className="btn-ghost" disabled={busy !== null} onClick={() => void go('portal', api.billingPortal)}>
              {busy === 'portal' ? 'Opening…' : 'Manage subscription & invoices'}
            </button>
          )}
        </div>
      </div>

      {paid && (
        <p className="pf-section-sub" style={{ marginTop: 10 }}>
          {me.paid_months} consecutive paid month{me.paid_months === 1 ? '' : 's'}.
          {nextTier
            ? ` ${nextTier.percent_off}% off from month ${nextTier.min_paid_months}.`
            : me.percent_off > 0
              ? ' Top loyalty tier reached.'
              : ''}{' '}
          Loyalty tenure resets if the subscription ends. Cancelling takes effect at the end of the paid period.
        </p>
      )}

      <div className="pf-grid" style={{ marginTop: 14 }}>
        <div className="pf-field">
          <label className="pf-label">Billing country</label>
          <select className="text-input" value={country} onChange={(e) => void saveCountry(e.target.value)}>
            <option value="">{me.country_source === 'detected' ? `Detected: ${countryName(me.country)}` : 'Detect automatically'}</option>
            {COUNTRIES.map(([code, name]) => (
              <option key={code} value={code}>{name}</option>
            ))}
          </select>
          <p className="pf-section-sub" style={{ marginTop: 6 }}>
            Sets the price you see. Changing it never reprices a subscription you already have.
          </p>
        </div>
        <div className="pf-field">
          <label className="pf-label">Hosted-model credit</label>
          <div className="text-input" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span>{dollars(me.credit_cents)}</span>
            <Link to="/app/settings" style={{ fontSize: 12.5, color: 'var(--accent)', fontWeight: 600, textDecoration: 'none' }}>
              Add credit
            </Link>
          </div>
        </div>
      </div>

      {status && (
        <span className={`pf-status ${status.kind === 'ok' ? 'pf-status-ok' : 'pf-status-bad'}`} style={{ display: 'block', marginTop: 10 }}>
          {status.text}
        </span>
      )}
    </>
  );
}

/** Export, delete, and the record of any support access. */
function DataSection({ account, onDeleted }: { account: api.Account; onDeleted: () => void }) {
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function remove() {
    setBusy(true);
    setError(null);
    try {
      await api.deleteAccount(password);
      onDeleted();
    } catch (err) {
      setError(err instanceof ApiError ? (err.status === 403 ? 'Password is wrong.' : err.message) : "Couldn't reach Aboyeur.");
      setBusy(false);
    }
  }

  return (
    <>
      {account.support_access.length > 0 && (
        <div className="au-note" style={{ marginBottom: 14 }}>
          <strong>Support access.</strong> An operator opened the full text of these conversations for a
          support case:{' '}
          {account.support_access.map((s) => `session ${s.session_id} on ${new Date(s.ts).toLocaleDateString()}`).join('; ')}.
        </div>
      )}
      <div className="pf-grid">
        <div className="pf-field">
          <label className="pf-label">Export</label>
          <a className="btn-ghost" href={api.exportUrl} style={{ textDecoration: 'none', display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}>
            Download my data (.zip)
          </a>
          <p className="pf-section-sub" style={{ marginTop: 6 }}>
            Every client, profile, plan, conversation and credit-ledger row as JSON.
          </p>
        </div>
        <div className="pf-field">
          <label className="pf-label">Delete account</label>
          <input className="text-input" type="password" autoComplete="current-password" placeholder="Your password" value={password} onChange={(e) => setPassword(e.target.value)} />
          <input className="text-input" placeholder='Type DELETE to confirm' value={confirm} onChange={(e) => setConfirm(e.target.value)} style={{ marginTop: 8 }} />
          <button type="button" className="btn-ghost" style={{ marginTop: 8, color: 'var(--danger)' }} disabled={busy || confirm !== 'DELETE'} onClick={() => void remove()}>
            {busy ? 'Deleting…' : 'Delete my account'}
          </button>
          <p className="pf-section-sub" style={{ marginTop: 6 }}>
            Cancels any subscription now. Data is inaccessible immediately and erased after 30 days.
          </p>
          {error && <span className="pf-status pf-status-bad">{error}</span>}
        </div>
      </div>
      <div style={{ marginTop: 14 }}>
        <ClinicalNote />
      </div>
    </>
  );
}

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
            <h2 className="pf-section-title">Plan &amp; billing</h2>
            <p className="pf-section-sub">
              Card entry, invoices and cancellation happen on Stripe's pages — we never see your card.
            </p>
          </div>
          <BillingSection />
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

        <section className="pf-section">
          <div>
            <h2 className="pf-section-title">Your data</h2>
            <p className="pf-section-sub">
              Client details and health profiles are encrypted at rest. Operators can't read a
              conversation the classifier flags as sensitive without it being logged here.{' '}
              <Link to="/legal/privacy" style={{ color: 'var(--accent)', fontWeight: 600, textDecoration: 'none' }}>Privacy policy</Link>
            </p>
          </div>
          <DataSection
            account={account}
            onDeleted={async () => {
              await logout();
              navigate('/', { replace: true });
            }}
          />
        </section>

        <div className="cl-danger-zone">
          <span>
            Signed in as {account.email}.{' '}
            <LegalFooter style={{ display: 'inline-flex', marginLeft: 8 }} />
          </span>
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
