import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import './Admin.css';
import * as api from '../../api';
import { count, dateTime, relativeTime, shortDate } from './format';

const dollars = (cents: number) => `$${(cents / 100).toFixed(2)}`;

/** Plan / comp / provider override / credit — the operator's levers on one
 * account. Every action is audited server-side. */
function BillingControls({ user, onChange }: { user: api.AdminUserDetail; onChange: (u: api.AdminUser) => void }) {
  const [providers, setProviders] = useState<api.LlmProvider[]>([]);
  const [credit, setCredit] = useState('');
  const [note, setNote] = useState('');
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.adminProviders().then(setProviders).catch(() => setProviders([]));
  }, []);

  async function run(fn: () => Promise<api.AdminUser>, ok: string) {
    setBusy(true);
    setMsg(null);
    try {
      onChange(await fn());
      setMsg(ok);
    } catch (err) {
      setMsg(err instanceof api.ApiError ? err.message : 'Failed.');
    } finally {
      setBusy(false);
    }
  }

  const comp = user.billing_status === 'comp';
  return (
    <div className="ad-card" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="ad-inline-form">
        <span className={`ad-tag ${user.plan === 'pro' ? 'ad-tag-success' : ''}`}>{user.plan}</span>
        <span className="ad-tag">{user.billing_status}</span>
        {user.paid_months > 0 && <span className="ad-cell-muted">{user.paid_months} paid months · tier {user.loyalty_tier ?? 'base'}</span>}
        {user.country && <span className="ad-cell-muted">{user.country}</span>}
        {!user.email_verified && <span className="ad-tag ad-tag-danger">unverified</span>}
        {user.deleted_at && <span className="ad-tag ad-tag-danger">deleted {shortDate(user.deleted_at)}</span>}
        <div style={{ flex: 1 }} />
        <button
          type="button"
          className="btn-ghost"
          disabled={busy}
          onClick={() => {
            if (!comp && user.has_stripe_customer && !window.confirm('Comping cancels any live Stripe subscription (prorated). Continue?')) return;
            void run(() => api.adminSetBilling(user.id, comp ? 'free' : 'comp'), comp ? 'Comp removed.' : 'Comped.');
          }}
        >
          {comp ? 'Remove comp' : 'Comp this account'}
        </button>
      </div>

      <div className="ad-inline-form">
        <span className="ad-cell-muted">LLM</span>
        <select
          className="text-input"
          value={user.llm_mode}
          disabled={busy}
          onChange={(e) => void run(() => api.adminSetUserLlm(user.id, { llm_mode: e.target.value as api.LlmMode }), 'Mode set.')}
        >
          <option value="byok">byok</option>
          <option value="hosted">hosted</option>
        </select>
        <span className="ad-cell-muted">provider</span>
        <select
          className="text-input"
          value={user.llm_provider_id ?? ''}
          disabled={busy}
          onChange={(e) =>
            void run(
              () => api.adminSetUserLlm(user.id, { llm_provider_id: e.target.value ? Number(e.target.value) : null }),
              'Provider set. Takes effect on their next turn.',
            )
          }
        >
          <option value="">platform default</option>
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}{p.enabled ? '' : ' (disabled)'}
            </option>
          ))}
        </select>
      </div>

      <div className="ad-inline-form">
        <span className="ad-cell-muted">Credit</span>
        <strong style={{ color: user.credit_cents < 0 ? 'var(--danger)' : undefined }}>{dollars(user.credit_cents)}</strong>
        <input className="text-input ad-input-sm" placeholder="± cents" value={credit} onChange={(e) => setCredit(e.target.value)} />
        <input className="text-input" placeholder="note (audited)" value={note} onChange={(e) => setNote(e.target.value)} style={{ width: 200 }} />
        <button
          type="button"
          className="btn-ghost"
          disabled={busy || !/^-?\d+$/.test(credit.trim())}
          onClick={() =>
            void run(() => api.adminAddCredit(user.id, Number(credit), note || undefined), 'Adjusted.').then(() => {
              setCredit('');
              setNote('');
            })
          }
        >
          Adjust
        </button>
      </div>
      {msg && <span className="ad-cell-muted">{msg}</span>}
    </div>
  );
}

function Ledger({ userId }: { userId: number }) {
  const [rows, setRows] = useState<api.LedgerRow[] | null>(null);
  useEffect(() => {
    api.adminUserLedger(userId).then(setRows).catch(() => setRows([]));
  }, [userId]);
  if (!rows) return <p className="ad-sub">Loading…</p>;
  if (rows.length === 0) return <p className="ad-sub">No credit movements yet.</p>;
  return (
    <div className="ad-card ad-card-pad0">
      <div className="ad-table-wrap">
        <table className="ad-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Kind</th>
              <th>Model</th>
              <th className="ad-num">In</th>
              <th className="ad-num">Out</th>
              <th className="ad-num">Cost</th>
              <th className="ad-num">Charged</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="ad-cell-muted" title={dateTime(r.ts)}>{relativeTime(r.ts)}</td>
                <td>
                  <span className={`ad-tag ${r.kind.endsWith('_est') ? 'ad-tag-danger' : ''}`}>{r.kind}</span>
                </td>
                <td className="ad-cell-muted">{r.provider_name ?? r.model}</td>
                <td className="ad-num">{count(r.tokens_in)}</td>
                <td className="ad-num">{count(r.tokens_out)}</td>
                <td className="ad-num">{dollars(r.cost_cents)}</td>
                <td className="ad-num" style={{ color: r.charge_cents < 0 ? 'var(--success)' : undefined }}>
                  {r.charge_cents < 0 ? '+' : ''}{dollars(-r.charge_cents)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function UserDetail() {
  const { userId } = useParams();
  const id = Number(userId);
  const [user, setUser] = useState<api.AdminUserDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setUser(null);
    setError(null);
    api
      .adminUser(id)
      .then((u) => !cancelled && setUser(u))
      .catch(() => !cancelled && setError("Couldn't load that account."));
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (error || !user) {
    return (
      <div className="ad-page">
        <div className="ad-inner">
          <Link to="/admin/users" className="ad-back">
            ← Users
          </Link>
          {error ? (
            <div className="ad-empty">
              <h2>Account unavailable</h2>
              <p>{error}</p>
            </div>
          ) : (
            <p className="ad-sub">Loading…</p>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="ad-page">
      <div className="ad-inner">
        <Link to="/admin/users" className="ad-back">
          ← Users
        </Link>

        <div className="ad-head">
          <div>
            <h1 className="ad-title">{user.email}</h1>
            <p className="ad-sub">
              {user.name || 'No name set'}
              {user.practice ? ` · ${user.practice}` : ''} · joined {shortDate(user.created_at)} ·
              last active {relativeTime(user.last_active)}
              {user.is_admin ? ' · operator' : ''}
            </p>
          </div>
        </div>

        <div className="ad-stats">
          <div className="ad-stat">
            <span className="ad-stat-label">Clients</span>
            <span className="ad-stat-value">{count(user.client_count)}</span>
          </div>
          <div className="ad-stat">
            <span className="ad-stat-label">Conversations</span>
            <span className="ad-stat-value">{count(user.session_count)}</span>
          </div>
          <div className="ad-stat">
            <span className="ad-stat-label">Messages</span>
            <span className="ad-stat-value">{count(user.message_count)}</span>
          </div>
          <div className="ad-stat">
            <span className="ad-stat-label">Errors (7d)</span>
            <span className={`ad-stat-value ${user.errors_7d > 0 ? 'ad-stat-value-danger' : 'ad-stat-value-muted'}`}>
              {user.errors_7d}
            </span>
          </div>
          <div className="ad-stat">
            <span className="ad-stat-label">★ Sensitive</span>
            <span className={`ad-stat-value ${user.sensitive_sessions > 0 ? '' : 'ad-stat-value-muted'}`}>
              {user.sensitive_sessions}
            </span>
          </div>
        </div>

        <h2 className="ad-section-title">Billing &amp; model</h2>
        <BillingControls user={user} onChange={(u) => setUser((prev) => (prev ? { ...prev, ...u } : prev))} />

        <h2 className="ad-section-title">Credit ledger</h2>
        <Ledger userId={user.id} />

        <h2 className="ad-section-title">Their clients</h2>
        <p className="ad-sub">Ids only — client names and profiles are the practitioner's clients' data, not ours to show.</p>
        {user.clients.length === 0 ? (
          <div className="ad-empty">
            <h2>No clients</h2>
            <p>This account hasn't finished onboarding — nothing to plan for yet.</p>
          </div>
        ) : (
          <div className="ad-card ad-card-pad0">
            <div className="ad-table-wrap">
              <table className="ad-table">
                <thead>
                  <tr>
                    <th>Client</th>
                    <th>Status</th>
                    <th className="ad-num">Conversations</th>
                    <th>Added</th>
                  </tr>
                </thead>
                <tbody>
                  {user.clients.map((c) => (
                    <tr key={c.id}>
                      <td className="ad-cell-strong">
                        #{c.id}
                        {c.is_self && <span className="ad-tag" style={{ marginLeft: 8 }}>Themselves</span>}
                      </td>
                      <td>
                        <span className={`ad-tag ${c.status === 'active' ? 'ad-tag-success' : ''}`}>
                          {c.status}
                        </span>
                      </td>
                      <td className="ad-num">{c.session_count}</td>
                      <td className="ad-cell-muted">{shortDate(c.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <h2 className="ad-section-title">Conversations</h2>
        {user.sessions.length === 0 ? (
          <div className="ad-empty">
            <h2>No conversations</h2>
            <p>Nothing has been said to the agent on this account yet.</p>
          </div>
        ) : (
          <div className="ad-card ad-card-pad0">
            <div className="ad-table-wrap">
              <table className="ad-table">
                <thead>
                  <tr>
                    <th>Conversation</th>
                    <th>Client</th>
                    <th className="ad-num">Messages</th>
                    <th>Last message</th>
                    <th>Started</th>
                  </tr>
                </thead>
                <tbody>
                  {user.sessions.map((s) => (
                    <tr key={s.id}>
                      <td>
                        {s.sensitive && <span className="ad-star" title="Contains flagged messages">★ </span>}
                        <Link to={`/admin/sessions/${s.id}`}>{s.title}</Link>
                      </td>
                      <td className="ad-cell-muted">#{s.client_id}</td>
                      <td className="ad-num">{s.message_count}</td>
                      <td className="ad-cell-muted" title={dateTime(s.last_message_at)}>
                        {relativeTime(s.last_message_at)}
                      </td>
                      <td className="ad-cell-muted">{shortDate(s.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
