import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import './Admin.css';
import * as api from '../../api';
import { STATUS_LABEL, dateTime, plainExcerpt, relativeTime, statusLabel } from './format';

const FILTERS = [
  { label: 'Open', value: 'open' },
  { label: 'New', value: 'new' },
  { label: 'In progress', value: 'in_progress' },
  { label: 'Resolved', value: 'resolved' },
  { label: 'All', value: '' },
];

const STATUSES = ['new', 'triaged', 'in_progress', 'resolved', 'wontfix'] as const;

function statusTone(status: string) {
  if (status === 'resolved') return 'ad-tag-success';
  if (status === 'new') return 'ad-tag-warn';
  if (status === 'wontfix') return '';
  return 'ad-tag-accent';
}

function Row({ item, onChanged }: { item: api.Feedback; onChanged: (f: api.Feedback) => void }) {
  const [note, setNote] = useState(item.admin_note ?? '');
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  // A row can be re-rendered with fresh server state (after a filter reload);
  // the note box must follow it rather than keep a stale local draft.
  useEffect(() => {
    setNote(item.admin_note ?? '');
  }, [item.id, item.admin_note]);

  async function patch(body: { status?: string; admin_note?: string }) {
    setBusy(true);
    try {
      onChanged(await api.adminUpdateFeedback(item.id, body));
      setSaved(true);
      setTimeout(() => setSaved(false), 1600);
    } finally {
      setBusy(false);
    }
  }

  const down = item.rating < 0;

  return (
    <div className={`ad-fb ${down ? 'ad-fb-down' : ''}`}>
      <div className="ad-fb-top">
        <span className={`ad-fb-rating ${down ? 'ad-fb-rating-down' : 'ad-fb-rating-up'}`}>
          {down ? '👎' : '👍'}
        </span>
        <span className="ad-fb-who">{item.email ?? `user ${item.user_id}`}</span>
        {item.client_name && <span className="ad-tag">{item.client_name}</span>}
        <span className="ad-fb-when" title={dateTime(item.created_at)}>
          {relativeTime(item.created_at)}
        </span>
        <span className={`ad-tag ${statusTone(item.status)}`}>{statusLabel(item.status)}</span>
        <div style={{ flex: 1 }} />
        {/* Deep-link to the exact turn, not just the conversation — the
            transcript rings the message this thumb was cast on. */}
        <Link to={`/admin/sessions/${item.session_id}?message=${item.message_id}`} className="ad-back">
          Open the turn →
        </Link>
      </div>

      {item.body && <p className="ad-fb-body">{item.body}</p>}

      {item.message_excerpt && <p className="ad-fb-quote">{plainExcerpt(item.message_excerpt)}</p>}

      <div className="ad-fb-actions">
        <select
          className="ad-select"
          value={item.status}
          disabled={busy}
          onChange={(e) => void patch({ status: e.target.value })}
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {STATUS_LABEL[s]}
            </option>
          ))}
        </select>
        <input
          className="ad-fb-note"
          placeholder="Note to self — what you found, what you did"
          value={note}
          disabled={busy}
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void patch({ admin_note: note })}
          onBlur={() => note !== (item.admin_note ?? '') && void patch({ admin_note: note })}
        />
        {saved && <span className="ad-fb-saved">Saved</span>}
      </div>
    </div>
  );
}

export default function Feedback() {
  const [filter, setFilter] = useState('open');
  const [items, setItems] = useState<api.Feedback[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setItems(null);
    setError(null);
    api
      .adminFeedback(filter || undefined)
      .then(setItems)
      .catch(() => setError("Couldn't load feedback."));
  }, [filter]);

  useEffect(load, [load]);

  function onChanged(updated: api.Feedback) {
    setItems((prev) => (prev ? prev.map((f) => (f.id === updated.id ? { ...f, ...updated } : f)) : prev));
  }

  return (
    <div className="ad-page">
      <div className="ad-inner">
        <div className="ad-head">
          <div>
            <h1 className="ad-title">Feedback</h1>
            <p className="ad-sub">
              Thumbs users left on individual answers. Thumb-downs with a comment are the ones
              worth reading; every row links back to the exact turn that earned it.
            </p>
          </div>
          <button type="button" className="btn-ghost" onClick={load}>
            Refresh
          </button>
        </div>

        <div className="ad-toolbar">
          <div className="ad-segmented">
            {FILTERS.map((f) => (
              <button key={f.value} type="button" aria-selected={filter === f.value} onClick={() => setFilter(f.value)}>
                {f.label}
              </button>
            ))}
          </div>
        </div>

        {error ? (
          <div className="ad-empty">
            <h2>Feedback unavailable</h2>
            <p>{error}</p>
          </div>
        ) : !items ? (
          <p className="ad-sub">Loading…</p>
        ) : items.length === 0 ? (
          <div className="ad-empty">
            <h2>{filter === 'open' ? 'Nothing open' : 'Nothing here'}</h2>
            <p>
              {filter === 'open'
                ? 'Every rated turn has been triaged.'
                : 'No feedback matches that filter yet.'}
            </p>
          </div>
        ) : (
          <div className="ad-fb-list">
            {items.map((f) => (
              <Row key={f.id} item={f} onChanged={onChanged} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
