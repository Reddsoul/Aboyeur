import { useEffect, useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './Admin.css';
import * as api from '../../api';
import { clockTime, dateTime, shortDate, statusLabel } from './format';

/** Read-only replay of one conversation, rendered in the same bubbles the
 * user saw. `?message=<id>` (how the feedback board deep-links) scrolls to and
 * rings the rated turn. */
export default function Transcript() {
  const { sessionId } = useParams();
  const id = Number(sessionId);
  const [params] = useSearchParams();
  const focusId = Number(params.get('message')) || null;

  const [data, setData] = useState<api.AdminTranscript | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Off by default; flipping it re-fetches with ?reveal=1, which the server
  // audits and shows to the affected user.
  const [reveal, setReveal] = useState(false);
  const focusRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    api
      .adminTranscript(id, reveal)
      .then((t) => !cancelled && setData(t))
      .catch(() => !cancelled && setError("Couldn't load that conversation."));
    return () => {
      cancelled = true;
    };
  }, [id, reveal]);

  // After the thread renders, not before — the node doesn't exist until then.
  useEffect(() => {
    if (data && focusRef.current) {
      focusRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [data]);

  if (error || !data) {
    return (
      <div className="ad-page">
        <div className="ad-inner">
          <Link to="/admin/users" className="ad-back">
            ← Users
          </Link>
          {error ? (
            <div className="ad-empty">
              <h2>Conversation unavailable</h2>
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
        <Link to={`/admin/users/${data.user_id}`} className="ad-back">
          ← {data.email}
        </Link>

        <div className="ad-head">
          <div>
            <h1 className="ad-title">{data.title}</h1>
            <p className="ad-sub">
              Client #{data.client_id} · {data.messages.length} message
              {data.messages.length === 1 ? '' : 's'} · started {shortDate(data.created_at)}
              {data.redacted_count > 0 && ` · ★ ${data.redacted_count} redacted`}
              {data.revealed && ' · revealed (audited)'}
            </p>
          </div>
          {data.redacted_count > 0 && !data.revealed && (
            <button
              type="button"
              className="btn-ghost"
              style={{ height: 32, padding: '0 14px', fontSize: 13 }}
              onClick={() => {
                if (
                  window.confirm(
                    'Reveal the redacted messages?\n\nThis is written to the audit log and the user will see ' +
                      '"Support viewed this conversation" with today\'s date on their Account page. Only do it for a real support case.',
                  )
                )
                  setReveal(true);
              }}
            >
              Reveal for support
            </button>
          )}
        </div>
        {data.redacted_count > 0 && !data.revealed && (
          <p className="ad-sub">
            Messages the classifier marked sensitive (or couldn't classify) are hidden, along with the
            reply to each. Unflagged lines have emails and phone numbers masked. This is a best-effort
            safeguard, not a compliance control — see docs/privacy-posture.md.
          </p>
        )}

        {data.messages.length === 0 ? (
          <div className="ad-empty">
            <h2>Empty conversation</h2>
            <p>This thread was started but nothing was ever said in it.</p>
          </div>
        ) : (
          <div className="ad-card">
            <div className="ad-thread">
              {data.messages.map((m) => {
                const focused = m.id === focusId;
                return (
                  <div
                    key={m.id}
                    ref={focused ? focusRef : undefined}
                    className={`ad-turn ${m.role === 'user' ? 'ad-turn-user' : 'ad-turn-assist'} ${
                      focused ? 'ad-turn-focus' : ''
                    }`}
                  >
                    <div className="ad-turn-meta" title={dateTime(m.created_at)}>
                      <span>{m.role === 'user' ? data.email : 'Aboyeur'}</span>
                      <span>·</span>
                      <span>{clockTime(m.created_at)}</span>
                    </div>
                    <div className={`ad-bubble ${m.role === 'user' ? 'ad-bubble-user' : 'ad-bubble-assist'}${m.redacted ? ' ad-bubble-redacted' : ''}`}>
                      {/* Assistant answers are markdown, the same as in Chat.
                          User messages are rendered as plain text — whatever
                          they typed is not markup, and treating it as such
                          would silently reformat the evidence. */}
                      {m.redacted ? (
                        <span>
                          ★ Redacted — {m.sensitivity === 'unknown' ? 'classifier could not label this message' : `flagged: ${m.categories.join(', ') || 'sensitive'}`}
                        </span>
                      ) : m.role === 'assistant' ? (
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content ?? ''}</ReactMarkdown>
                      ) : (
                        m.content
                      )}
                    </div>
                    {!m.redacted && m.sensitivity === 'sensitive' && (
                      <div className="ad-turn-meta">★ {m.categories.join(', ')}</div>
                    )}
                    {m.feedback && (
                      <div
                        className={`ad-turn-feedback ${
                          m.feedback.rating < 0 ? 'ad-turn-feedback-down' : ''
                        }`}
                      >
                        <span>{m.feedback.rating < 0 ? '👎' : '👍'}</span>
                        <span>
                          <b>{m.feedback.rating < 0 ? 'Thumbs down' : 'Thumbs up'}</b>
                          {m.feedback.body ? ` — ${m.feedback.body}` : ''}
                          {' · '}
                          {statusLabel(m.feedback.status)}
                        </span>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
