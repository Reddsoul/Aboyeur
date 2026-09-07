import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import './Clients.css';
import * as api from '../api';
import { ApiError } from '../api';
import { useClients } from '../clients/ClientContext';

function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? '')
    .join('');
}

function relativeDate(iso: string | null) {
  if (!iso) return 'no plans yet';
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);
  if (Number.isNaN(days)) return 'no plans yet';
  if (days <= 0) return 'planned today';
  if (days === 1) return 'planned yesterday';
  if (days < 30) return `planned ${days}d ago`;
  return `planned ${Math.floor(days / 30)}mo ago`;
}

export default function Clients() {
  const navigate = useNavigate();
  const { clients, loading, activeClient, setActiveClient, refreshClients } = useClients();

  const [query, setQuery] = useState('');
  const [showArchived, setShowArchived] = useState(false);
  const [archived, setArchived] = useState<api.Client[]>([]);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({ name: '', email: '', phone: '', notes: '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const shown = useMemo(() => {
    const pool = showArchived ? [...clients, ...archived.filter((a) => a.status === 'archived')] : clients;
    const q = query.trim().toLowerCase();
    if (!q) return pool;
    return pool.filter((c) =>
      [c.name, c.email ?? '', c.notes ?? ''].some((f) => f.toLowerCase().includes(q)),
    );
  }, [clients, archived, showArchived, query]);

  async function toggleArchived() {
    const next = !showArchived;
    setShowArchived(next);
    if (next && archived.length === 0) {
      try {
        setArchived(await api.listClients(true));
      } catch {
        setError("Couldn't load archived clients.");
      }
    }
  }

  async function add() {
    const name = draft.name.trim();
    if (!name) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.createClient({
        name,
        email: draft.email.trim() || undefined,
        phone: draft.phone.trim() || undefined,
        notes: draft.notes.trim() || undefined,
      });
      await refreshClients();
      setActiveClient(created);
      setDraft({ name: '', email: '', phone: '', notes: '' });
      setAdding(false);
      // Straight into their profile — a client with no anthropometrics can't
      // be planned for, so that's the next thing to do every time.
      navigate(`/app/clients/${created.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't add them (server said ${err.status}).` : "Couldn't add them.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="cl-page">
      <div className="cl-inner">
        <div className="cl-head">
          <div>
            <h1 className="cl-title">Clients</h1>
            <p className="cl-sub">
              Everyone you plan for. Each one keeps their own profile, targets and plans — nothing
              carries between them.
            </p>
          </div>
          <button type="button" className="btn-primary" style={{ height: 42, padding: '0 22px', fontSize: 15 }} onClick={() => setAdding((v) => !v)}>
            {adding ? 'Cancel' : 'Add client'}
          </button>
        </div>

        {adding && (
          <div className="cl-form">
            <div className="cl-form-row">
              <input
                className="text-input"
                autoFocus
                placeholder="Full name"
                value={draft.name}
                onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
                onKeyDown={(e) => e.key === 'Enter' && void add()}
              />
              <input
                className="text-input"
                type="email"
                placeholder="Email (optional)"
                value={draft.email}
                onChange={(e) => setDraft((d) => ({ ...d, email: e.target.value }))}
              />
              <input
                className="text-input"
                placeholder="Phone (optional)"
                value={draft.phone}
                onChange={(e) => setDraft((d) => ({ ...d, phone: e.target.value }))}
              />
            </div>
            <textarea
              className="text-input"
              placeholder="Notes — training block, injuries, what they're working on…"
              value={draft.notes}
              onChange={(e) => setDraft((d) => ({ ...d, notes: e.target.value }))}
            />
            <div className="cl-form-actions">
              <button type="button" className="btn-primary" disabled={!draft.name.trim() || busy} onClick={() => void add()}>
                {busy ? 'Adding…' : 'Add and open profile'}
              </button>
              {error && <span style={{ color: 'var(--danger)', fontSize: 13.5 }}>{error}</span>}
            </div>
          </div>
        )}

        <div className="cl-toolbar">
          <input
            className="text-input cl-search"
            placeholder="Search clients"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button type="button" className="btn-ghost" onClick={() => void toggleArchived()}>
            {showArchived ? 'Hide archived' : 'Show archived'}
          </button>
        </div>

        {loading ? (
          <p className="cl-sub">Loading your book…</p>
        ) : shown.length === 0 ? (
          <div className="cl-empty">
            <h2>{clients.length === 0 ? 'No clients yet' : 'Nothing matches that search'}</h2>
            <p>
              {clients.length === 0
                ? 'Add the first person you plan for. Their profile, targets and plans all live under them.'
                : 'Try a different name or email.'}
            </p>
          </div>
        ) : (
          <div className="cl-grid">
            {shown.map((c) => (
              <button
                key={c.id}
                type="button"
                className={`cl-card ${activeClient?.id === c.id ? 'cl-card-active' : ''} ${c.status === 'archived' ? 'cl-card-archived' : ''}`}
                onClick={() => {
                  setActiveClient(c);
                  navigate(`/app/clients/${c.id}`);
                }}
              >
                <div className="cl-card-top">
                  <div className="cl-avatar">{initials(c.name) || '?'}</div>
                  <div style={{ minWidth: 0 }}>
                    <p className="cl-name">{c.name}</p>
                    <p className="cl-meta">{c.email || relativeDate(c.last_plan_at)}</p>
                  </div>
                </div>

                <div className="cl-stats">
                  <span>
                    <b>{c.daily_kcal ?? '—'}</b> kcal/day
                  </span>
                  <span>
                    <b>{c.menu_count}</b> menus
                  </span>
                  <span>
                    <b>{c.kit_count}</b> kits
                  </span>
                </div>

                <div className="cl-tags">
                  {activeClient?.id === c.id && <span className="cl-tag cl-tag-active">Active</span>}
                  {c.is_self && <span className="cl-tag">You</span>}
                  {c.status === 'archived' && <span className="cl-tag">Archived</span>}
                  {c.goal && <span className="cl-tag">{c.goal}</span>}
                  {!c.has_profile && <span className="cl-tag">Profile needed</span>}
                  {c.allergies.slice(0, 2).map((a) => (
                    <span className="cl-tag cl-tag-danger" key={a}>
                      {a}
                    </span>
                  ))}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
