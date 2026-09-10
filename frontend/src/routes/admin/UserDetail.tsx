import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import './Admin.css';
import * as api from '../../api';
import { count, dateTime, relativeTime, shortDate } from './format';

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
        </div>

        <h2 className="ad-section-title">Their clients</h2>
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
                        {c.name}
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
                        <Link to={`/admin/sessions/${s.id}`}>{s.title}</Link>
                      </td>
                      <td className="ad-cell-muted">{s.client_name}</td>
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
