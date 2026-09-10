import { useCallback, useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import './Admin.css';
import * as api from '../../api';
import { dateTime, duration } from './format';

const RANGES = [
  { label: '24h', days: 1 },
  { label: '7d', days: 7 },
  { label: '30d', days: 30 },
  { label: 'All', days: undefined as number | undefined },
];

function EventRow({ event }: { event: api.AdminEvent }) {
  const [open, setOpen] = useState(false);
  const failed = !event.ok || event.kind === 'error';

  return (
    <>
      <tr>
        <td className="ad-cell-muted" style={{ whiteSpace: 'nowrap' }}>
          {dateTime(event.ts)}
        </td>
        <td>
          {failed ? (
            <span className="ad-tag ad-tag-danger">{event.error_type || event.kind}</span>
          ) : (
            <span className="ad-tag">{event.kind}</span>
          )}
        </td>
        <td>
          {event.email ? (
            <Link to={`/admin/users/${event.user_id}`}>{event.email}</Link>
          ) : (
            <span className="ad-cell-muted">—</span>
          )}
        </td>
        <td className="ad-num">{duration(event.latency_ms)}</td>
        <td className="ad-num">{event.tool_calls ?? '—'}</td>
        <td className="ad-num">
          {/* Tokens stay null whenever the endpoint doesn't report usage —
              "—" says unknown, which is different from zero. */}
          {event.tokens_in == null && event.tokens_out == null
            ? '—'
            : `${event.tokens_in ?? '?'} / ${event.tokens_out ?? '?'}`}
        </td>
        <td>
          {event.session_id ? (
            <Link to={`/admin/sessions/${event.session_id}`}>Transcript</Link>
          ) : (
            <span className="ad-cell-muted">—</span>
          )}
          {event.error_detail && (
            <button type="button" className="ad-expand" style={{ marginLeft: 10 }} onClick={() => setOpen((v) => !v)}>
              {open ? 'Hide' : 'Traceback'}
            </button>
          )}
        </td>
      </tr>
      {open && event.error_detail && (
        <tr>
          <td colSpan={7} style={{ paddingTop: 0 }}>
            <pre className="ad-pre">{event.error_detail}</pre>
          </td>
        </tr>
      )}
    </>
  );
}

export default function Activity() {
  const [params, setParams] = useSearchParams();
  const errorsOnly = params.get('errors') === '1';
  const [days, setDays] = useState<number | undefined>(7);
  const [events, setEvents] = useState<api.AdminEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState<string[] | null>(null);
  const [logOpen, setLogOpen] = useState(false);

  const load = useCallback(() => {
    setEvents(null);
    setError(null);
    api
      .adminEvents({ errorsOnly, days, limit: 300 })
      .then(setEvents)
      .catch(() => setError("Couldn't load the event log."));
  }, [errorsOnly, days]);

  useEffect(load, [load]);

  function setErrorsOnly(next: boolean) {
    // Kept in the URL so the dashboard's "open errors" alert can deep-link
    // straight into the filtered view.
    const p = new URLSearchParams(params);
    if (next) p.set('errors', '1');
    else p.delete('errors');
    setParams(p, { replace: true });
  }

  async function toggleLog() {
    const next = !logOpen;
    setLogOpen(next);
    if (next && log === null) {
      try {
        setLog((await api.adminLogTail(300)).lines);
      } catch {
        setLog(["Couldn't read the log file."]);
      }
    }
  }

  return (
    <div className="ad-page">
      <div className="ad-inner">
        <div className="ad-head">
          <div>
            <h1 className="ad-title">Activity</h1>
            <p className="ad-sub">
              One row per agent turn. Failed turns carry the traceback and the conversation they
              died in — no SSH, no tailing.
            </p>
          </div>
          <button type="button" className="btn-ghost" onClick={load}>
            Refresh
          </button>
        </div>

        <div className="ad-toolbar">
          <div className="ad-segmented">
            <button type="button" aria-selected={!errorsOnly} onClick={() => setErrorsOnly(false)}>
              All turns
            </button>
            <button type="button" aria-selected={errorsOnly} onClick={() => setErrorsOnly(true)}>
              Errors only
            </button>
          </div>
          <div className="ad-segmented">
            {RANGES.map((r) => (
              <button key={r.label} type="button" aria-selected={days === r.days} onClick={() => setDays(r.days)}>
                {r.label}
              </button>
            ))}
          </div>
          <div style={{ flex: 1 }} />
          <button type="button" className="btn-ghost" onClick={() => void toggleLog()}>
            {logOpen ? 'Hide raw log' : 'Raw uvicorn log'}
          </button>
        </div>

        {logOpen && (
          <div className="ad-card">
            <div className="ad-chart-head">
              <span className="ad-chart-title">logs/uvicorn.log</span>
              <span className="ad-chart-note">last 300 lines</span>
            </div>
            <pre className="ad-pre">{log === null ? 'Loading…' : log.join('\n') || 'Log is empty.'}</pre>
          </div>
        )}

        {error ? (
          <div className="ad-empty">
            <h2>Event log unavailable</h2>
            <p>{error}</p>
          </div>
        ) : !events ? (
          <p className="ad-sub">Loading…</p>
        ) : events.length === 0 ? (
          <div className="ad-empty">
            <h2>{errorsOnly ? 'No errors in this window' : 'No events in this window'}</h2>
            <p>
              {errorsOnly
                ? 'Nothing has failed in the range you picked.'
                : 'Turns are recorded from the moment this build went live — an older deploy left no rows behind.'}
            </p>
          </div>
        ) : (
          <div className="ad-card ad-card-pad0">
            <div className="ad-table-wrap">
              <table className="ad-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Kind</th>
                    <th>Account</th>
                    <th className="ad-num">Latency</th>
                    <th className="ad-num">Tools</th>
                    <th className="ad-num">Tokens in / out</th>
                    <th>Links</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((e) => (
                    <EventRow key={e.id} event={e} />
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
