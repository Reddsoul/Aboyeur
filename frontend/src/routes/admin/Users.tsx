import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import './Admin.css';
import * as api from '../../api';
import { count, relativeTime, shortDate } from './format';

export default function Users() {
  const [users, setUsers] = useState<api.AdminUser[] | null>(null);
  const [query, setQuery] = useState('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .adminUsers()
      .then((u) => !cancelled && setUsers(u))
      .catch(() => !cancelled && setError("Couldn't load users."));
    return () => {
      cancelled = true;
    };
  }, []);

  const shown = useMemo(() => {
    if (!users) return [];
    const q = query.trim().toLowerCase();
    if (!q) return users;
    return users.filter((u) =>
      [u.email, u.name ?? '', u.practice ?? ''].some((f) => f.toLowerCase().includes(q)),
    );
  }, [users, query]);

  return (
    <div className="ad-page">
      <div className="ad-inner">
        <div className="ad-head">
          <div>
            <h1 className="ad-title">Users</h1>
            <p className="ad-sub">
              Every account on this deploy, most recently active first. "Last active" is their
              newest chat message — an account that only logs in doesn't count as using it.
            </p>
          </div>
        </div>

        <div className="ad-toolbar">
          <input
            className="text-input ad-search"
            placeholder="Search by email, name or practice"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        {error ? (
          <div className="ad-empty">
            <h2>Users unavailable</h2>
            <p>{error}</p>
          </div>
        ) : !users ? (
          <p className="ad-sub">Loading…</p>
        ) : shown.length === 0 ? (
          <div className="ad-empty">
            <h2>{users.length === 0 ? 'No accounts yet' : 'Nothing matches that search'}</h2>
            <p>
              {users.length === 0
                ? 'Provision one with `aboyeur create-user <email>`.'
                : 'Try a different email or name.'}
            </p>
          </div>
        ) : (
          <div className="ad-card ad-card-pad0">
            <div className="ad-table-wrap">
              <table className="ad-table">
                <thead>
                  <tr>
                    <th>Account</th>
                    <th>Plan</th>
                    <th>LLM</th>
                    <th className="ad-num">Credit</th>
                    <th>Last active</th>
                    <th className="ad-num">Clients</th>
                    <th className="ad-num">Conversations</th>
                    <th className="ad-num">Messages</th>
                    <th className="ad-num">Errors 7d</th>
                    <th>Joined</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((u) => (
                    <tr key={u.id}>
                      <td>
                        <Link to={`/admin/users/${u.id}`}>{u.email}</Link>
                        <div className="ad-cell-muted">
                          {u.name || 'no name set'}
                          {u.country ? ` · ${u.country}` : ''}
                          {u.is_admin && <span className="ad-tag ad-tag-accent" style={{ marginLeft: 8 }}>Admin</span>}
                          {!u.email_verified && <span className="ad-tag ad-tag-danger" style={{ marginLeft: 8 }}>unverified</span>}
                          {u.deleted_at && <span className="ad-tag ad-tag-danger" style={{ marginLeft: 8 }}>deleted</span>}
                          {u.sensitive_sessions > 0 && (
                            <span className="ad-star" style={{ marginLeft: 8 }} title={`${u.sensitive_sessions} sensitive conversation(s)`}>
                              ★ {u.sensitive_sessions}
                            </span>
                          )}
                        </div>
                      </td>
                      <td>
                        <span className={`ad-tag ${u.plan === 'pro' ? 'ad-tag-success' : ''}`}>{u.billing_status === 'comp' ? 'comp' : u.plan}</span>
                        {u.billing_status === 'past_due' && <span className="ad-tag ad-tag-danger" style={{ marginLeft: 6 }}>past due</span>}
                        {u.paid_months > 0 && <div className="ad-cell-muted">{u.paid_months} mo{u.loyalty_tier ? ` · ${u.loyalty_tier}` : ''}</div>}
                      </td>
                      <td className="ad-cell-muted">
                        {u.llm_mode}
                        {u.llm_provider_id != null && ` · p${u.llm_provider_id}`}
                      </td>
                      <td className="ad-num" style={{ color: u.credit_cents < 0 ? 'var(--danger)' : undefined }}>
                        {u.llm_mode === 'hosted' || u.credit_cents !== 0 ? `$${(u.credit_cents / 100).toFixed(2)}` : <span className="ad-cell-muted">—</span>}
                      </td>
                      <td>
                        <span className={u.last_active ? '' : 'ad-cell-muted'}>
                          {relativeTime(u.last_active)}
                        </span>
                      </td>
                      <td className="ad-num">{count(u.client_count)}</td>
                      <td className="ad-num">{count(u.session_count)}</td>
                      <td className="ad-num">{count(u.message_count)}</td>
                      <td className="ad-num">
                        {u.errors_7d > 0 ? (
                          <span className="ad-tag ad-tag-danger">{u.errors_7d}</span>
                        ) : (
                          <span className="ad-cell-muted">0</span>
                        )}
                      </td>
                      <td className="ad-cell-muted">{shortDate(u.created_at)}</td>
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
