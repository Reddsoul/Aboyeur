import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import './Clients.css';
import './Profile.css';
import * as api from '../api';
import { ApiError } from '../api';
import { useClients } from '../clients/ClientContext';
import Profile from './Profile';
import { DisclaimerLink } from '../components/Legal';

type Tab = 'profile' | 'plans' | 'details';

function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]?.toUpperCase() ?? '').join('');
}

const dateLabel = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
};

const sizeLabel = (bytes: number) =>
  bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

const timeLabel = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
};

/** "Export PDF" on a menu card plus the snapshots already taken. Each
 * export is stored server-side as sent, so a later swap or pantry change
 * never rewrites what the client already has. */
function MenuExports({
  menu,
  clientId,
  onChanged,
}: {
  menu: api.MenuSummary;
  clientId: number;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function exportNow() {
    setBusy(true);
    setError(null);
    try {
      await api.exportMenuAndDownload(menu.id, clientId);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't build the PDF.");
    } finally {
      setBusy(false);
    }
  }

  async function remove(exportId: number) {
    try {
      await api.deleteExport(exportId, clientId);
      onChanged();
    } catch {
      setError("Couldn't delete that export.");
    }
  }

  return (
    <div className="cl-exports">
      <button type="button" className="cl-export-btn" onClick={exportNow} disabled={busy}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 3v12m0 0 4-4m-4 4-4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
        </svg>
        {busy ? 'Building…' : 'Export PDF'}
      </button>
      {error && <p className="cl-export-error">{error}</p>}
      {menu.exports.length > 0 && (
        <ul className="cl-export-list">
          {menu.exports.map((e) => (
            <li key={e.id}>
              <a href={api.exportDownloadUrl(e.id, clientId)} download={e.filename}>
                PDF · {timeLabel(e.created_at)}
              </a>
              <span>{sizeLabel(e.size_bytes)}</span>
              <button type="button" title="Delete this export" aria-label="Delete this export" onClick={() => remove(e.id)}>
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Plans({ clientId, clientName }: { clientId: number; clientName: string }) {
  const [plans, setPlans] = useState<{ menus: api.MenuSummary[]; kits: api.KitSummary[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .getClientPlans(clientId)
      .then(setPlans)
      .catch(() => setError("Couldn't load their plans."));
  }, [clientId]);

  useEffect(() => {
    setPlans(null);
    load();
  }, [load]);

  if (error) return <p className="pf-sub">{error}</p>;
  if (!plans) return <p className="pf-sub">Loading plans…</p>;

  const empty = plans.menus.length === 0 && plans.kits.length === 0;

  return (
    <section className="pf-section">
      <div>
        <h2 className="pf-section-title">Plans</h2>
        <p className="pf-section-sub">
          Everything built for {clientName}. Plans belong to one client — nothing here was derived
          from, or feeds into, anyone else's.
        </p>
        <DisclaimerLink />
      </div>

      {empty ? (
        <div className="cl-empty">
          <h2>No plans yet</h2>
          <p>
            Open Chat with {clientName} active and ask for a week of dinners, or a macro kit. What
            you build lands here.
          </p>
        </div>
      ) : (
        <div className="cl-plan-list">
          {plans.menus.map((m) => (
            <div className="cl-plan cl-plan-menu" key={`menu-${m.id}`}>
              <div className="cl-plan-row">
                <div className="cl-plan-icon">
                  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M8 2v4m8-4v4M3 9h18M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z" />
                  </svg>
                </div>
                <div className="cl-plan-main">
                  <strong>Menu · week of {m.week_of}</strong>
                  <span>
                    {m.days} day{m.days === 1 ? '' : 's'} · {m.meals} meal{m.meals === 1 ? '' : 's'} ·{' '}
                    {m.status} · added {dateLabel(m.created_at)}
                  </span>
                </div>
                <span className="cl-tag">#{m.id}</span>
              </div>
              <MenuExports menu={m} clientId={clientId} onChanged={load} />
            </div>
          ))}
          {plans.kits.map((k) => (
            <div className="cl-plan" key={`kit-${k.id}`}>
              <div className="cl-plan-icon">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 3v18M4 7h16M6 7l-3 6a3 3 0 0 0 6 0l-3-6Zm12 0l-3 6a3 3 0 0 0 6 0l-3-6Z" />
                </svg>
              </div>
              <div className="cl-plan-main">
                <strong>Macro kit{k.strategy ? ` · ${k.strategy}` : ''}</strong>
                <span>
                  {k.component_count} foods
                  {k.days ? ` · ${k.days} days` : ''}
                  {k.meals_per_day ? ` · ${k.meals_per_day}/day` : ''} · added {dateLabel(k.created_at)}
                </span>
              </div>
              <span className="cl-tag">#{k.id}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function Details({ client, onChanged }: { client: api.Client; onChanged: () => void }) {
  const navigate = useNavigate();
  const [form, setForm] = useState({
    name: client.name,
    email: client.email ?? '',
    phone: client.phone ?? '',
    notes: client.notes ?? '',
  });
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [confirmingPurge, setConfirmingPurge] = useState(false);

  const patch = (p: Partial<typeof form>) => {
    setForm((f) => ({ ...f, ...p }));
    setDirty(true);
    setStatus(null);
  };

  async function save() {
    setSaving(true);
    try {
      await api.updateClient(client.id, {
        name: form.name,
        email: form.email || null,
        phone: form.phone || null,
        notes: form.notes || null,
      });
      onChanged();
      setDirty(false);
      setStatus('Saved.');
    } catch (err) {
      setStatus(err instanceof ApiError ? `Couldn't save (server said ${err.status}).` : "Couldn't save.");
    } finally {
      setSaving(false);
    }
  }

  async function setStatusFlag(next: 'active' | 'archived') {
    await api.updateClient(client.id, { status: next });
    onChanged();
  }

  async function purge() {
    await api.removeClient(client.id, true);
    onChanged();
    navigate('/app/clients', { replace: true });
  }

  return (
    <>
      <section className="pf-section">
        <div>
          <h2 className="pf-section-title">Details</h2>
          <p className="pf-section-sub">Contact info and your own notes. Never shown to the model.</p>
        </div>
        <div className="pf-grid">
          <div className="pf-field">
            <label className="pf-label">Name</label>
            <input className="text-input" value={form.name} onChange={(e) => patch({ name: e.target.value })} />
          </div>
          <div className="pf-field">
            <label className="pf-label">Email</label>
            <input className="text-input" type="email" value={form.email} onChange={(e) => patch({ email: e.target.value })} />
          </div>
          <div className="pf-field">
            <label className="pf-label">Phone</label>
            <input className="text-input" value={form.phone} onChange={(e) => patch({ phone: e.target.value })} />
          </div>
        </div>
        <div className="pf-field">
          <label className="pf-label">Notes</label>
          <textarea
            className="text-input"
            style={{ height: 'auto', minHeight: 96, padding: '10px 14px', resize: 'vertical' }}
            value={form.notes}
            onChange={(e) => patch({ notes: e.target.value })}
            placeholder="Training block, injuries, adherence, what to revisit next session…"
          />
        </div>
        <div className="pf-savebar" style={{ position: 'static' }}>
          <button type="button" className="btn-primary" disabled={!dirty || saving} onClick={() => void save()}>
            {saving ? 'Saving…' : 'Save details'}
          </button>
          <span className="pf-status">{status ?? (dirty ? 'Unsaved changes.' : 'Everything saved.')}</span>
        </div>
      </section>

      <div className="cl-danger-zone">
        {client.status === 'archived' ? (
          <>
            <span>This client is archived — hidden from the roster, data intact.</span>
            <button type="button" className="btn-ghost" onClick={() => void setStatusFlag('active')}>
              Restore
            </button>
          </>
        ) : (
          <>
            <span>Archiving hides them from the roster and keeps everything.</span>
            <button type="button" className="btn-ghost" onClick={() => void setStatusFlag('archived')}>
              Archive
            </button>
          </>
        )}
        {confirmingPurge ? (
          <>
            <span style={{ color: 'var(--danger)' }}>Delete {client.name} and every plan permanently?</span>
            <button type="button" className="cl-btn-danger" onClick={() => void purge()}>
              Yes, delete
            </button>
            <button type="button" className="btn-ghost" onClick={() => setConfirmingPurge(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" className="cl-btn-danger" onClick={() => setConfirmingPurge(true)}>
            Delete permanently
          </button>
        )}
      </div>
    </>
  );
}

export default function ClientDetail() {
  const { clientId } = useParams();
  const id = Number(clientId);
  const navigate = useNavigate();
  const { clients, activeClient, setActiveClient, refreshClients } = useClients();

  const [client, setClient] = useState<api.Client | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [tab, setTab] = useState<Tab>('profile');

  const load = useCallback(async () => {
    try {
      setClient(await api.getClient(id));
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setNotFound(true);
    }
  }, [id]);

  useEffect(() => {
    if (!Number.isFinite(id)) {
      setNotFound(true);
      return;
    }
    // Show the roster's copy immediately, then confirm against the server —
    // which is also what rejects an id from someone else's book.
    setClient(clients.find((c) => c.id === id) ?? null);
    void load();
  }, [id, clients, load]);

  const onChanged = useCallback(() => {
    void load();
    void refreshClients();
  }, [load, refreshClients]);

  if (notFound) {
    return (
      <div className="cl-page">
        <div className="cl-inner">
          <div className="cl-empty">
            <h2>No such client</h2>
            <p>They may have been deleted, or they belong to another account.</p>
            <Link className="cl-back" to="/app/clients">
              Back to clients
            </Link>
          </div>
        </div>
      </div>
    );
  }

  if (!client) {
    return (
      <div className="cl-page">
        <div className="cl-inner">
          <p className="cl-sub">Loading client…</p>
        </div>
      </div>
    );
  }

  const isActive = activeClient?.id === client.id;

  return (
    <div className="cl-page">
      <div className="cl-inner">
        <Link className="cl-back" to="/app/clients">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M15 18l-6-6 6-6" />
          </svg>
          All clients
        </Link>

        <div className="cl-ws-head">
          <div className="cl-avatar">{initials(client.name) || '?'}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h1 className="cl-title" style={{ margin: 0 }}>
              {client.name}
            </h1>
            <p className="cl-sub">
              {client.daily_kcal ? `${client.daily_kcal} kcal/day` : 'No targets yet'}
              {client.goal ? ` · ${client.goal}` : ''} · {client.menu_count} menus · {client.kit_count} kits
              {client.status === 'archived' ? ' · archived' : ''}
            </p>
          </div>
          {isActive ? (
            <button
              type="button"
              className="btn-primary"
              style={{ height: 40, padding: '0 20px', fontSize: 14.5 }}
              onClick={() => navigate('/app/chat')}
            >
              Plan for {client.name.split(' ')[0]}
            </button>
          ) : (
            <button
              type="button"
              className="btn-ghost"
              onClick={() => {
                setActiveClient(client);
                navigate('/app/chat');
              }}
            >
              Make active and plan
            </button>
          )}
        </div>

        <div className="cl-tabs" role="tablist">
          {(['profile', 'plans', 'details'] as Tab[]).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
              {t === 'profile' ? 'Profile' : t === 'plans' ? 'Plans' : 'Details'}
            </button>
          ))}
        </div>

        {tab === 'profile' && <Profile clientId={client.id} clientName={client.name} onSaved={onChanged} />}
        {tab === 'plans' && <Plans clientId={client.id} clientName={client.name} />}
        {tab === 'details' && <Details client={client} onChanged={onChanged} />}
      </div>
    </div>
  );
}
