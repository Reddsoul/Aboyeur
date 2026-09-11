import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './Chat.css';
import * as api from '../api';
import { useAuth } from '../auth/AuthContext';
import { useClients } from '../clients/ClientContext';
import Logo from '../components/Logo';

interface Message {
  /** Render key only — a local counter, unique per mount. */
  id: number;
  /** The row id in chat_messages. Absent for the synthetic greeting and for
   * a turn that is still streaming, which is exactly when a thumb has
   * nothing to attach itself to — so the buttons wait for it. */
  serverId?: number;
  from: 'user' | 'assistant';
  text: string;
}

const SUGGESTIONS = ['Plan my week', 'Find a quick high-protein dinner', 'What can you help me with?'];

let idCounter = 1;

const greeting = (name: string): Message => ({
  id: idCounter++,
  from: 'assistant',
  text: `Planning for **${name}**. Ask for a week of dinners, a macro kit, or a swap — I'll use their profile, targets and allergies only.`,
});

function toMessages(history: api.ChatMessage[], clientName: string): Message[] {
  if (!history.length) return [greeting(clientName)];
  return history.map((m) => ({
    id: idCounter++,
    serverId: m.id,
    from: m.role === 'user' ? ('user' as const) : ('assistant' as const),
    text: m.content,
  }));
}

/** Thumb buttons under one assistant turn. Thumb-down opens an optional
 * "what went wrong?" box — optional on purpose: forcing a comment costs the
 * signal from everyone who won't type one. */
function FeedbackBar({
  rating,
  onRate,
  onClear,
}: {
  rating: number | undefined;
  onRate: (rating: number) => void;
  onClear: () => void;
}) {
  return (
    <div className="chat-feedback">
      <button
        type="button"
        className={`chat-fb-btn ${rating === 1 ? 'chat-fb-btn-up' : ''}`}
        title={rating === 1 ? 'Remove your rating' : 'This answer was good'}
        aria-pressed={rating === 1}
        onClick={() => (rating === 1 ? onClear() : onRate(1))}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M7 10v11H4a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1h3Zm0 0 5-8a2.5 2.5 0 0 1 2.4 3.2L13.5 9h5.3a2 2 0 0 1 2 2.5l-1.7 7A2 2 0 0 1 17 20H7" />
        </svg>
      </button>
      <button
        type="button"
        className={`chat-fb-btn ${rating === -1 ? 'chat-fb-btn-down' : ''}`}
        title={rating === -1 ? 'Remove your rating' : 'Something was wrong with this'}
        aria-pressed={rating === -1}
        onClick={() => (rating === -1 ? onClear() : onRate(-1))}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M17 14V3h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-3Zm0 0-5 8a2.5 2.5 0 0 1-2.4-3.2L10.5 15H5.2a2 2 0 0 1-2-2.5l1.7-7A2 2 0 0 1 7 4h10" />
        </svg>
      </button>
    </div>
  );
}

export default function Chat() {
  const { llmConfigured } = useAuth();
  const { activeClient } = useClients();
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [sessions, setSessions] = useState<api.ChatSession[]>([]);
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  // { [chat_messages.id]: -1 | 1 } — this user's own thumbs in this thread.
  const [ratings, setRatings] = useState<Record<number, number>>({});
  // The message whose thumb-down comment box is open, if any.
  const [commentFor, setCommentFor] = useState<number | null>(null);
  const [commentDraft, setCommentDraft] = useState('');
  const threadRef = useRef<HTMLDivElement>(null);
  // The conversation currently on screen, readable from inside a long-lived
  // catch-up stream so it stops touching the thread if the user moves on.
  const sessionIdRef = useRef<number | null>(null);
  sessionIdRef.current = sessionId;

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, streaming]);

  async function loadRatings(id: number, cancelled?: () => boolean) {
    try {
      const raw = await api.getSessionFeedback(id);
      if (cancelled?.()) return;
      setRatings(Object.fromEntries(Object.entries(raw).map(([k, v]) => [Number(k), v])));
    } catch {
      /* thumbs are an extra; the conversation reads fine without them */
    }
  }

  /** A refresh mid-answer used to look like nothing had happened: the turn
   * kept the server busy but the screen showed the question with no reply.
   * Turns now outlive the connection, so on opening a conversation ask
   * whether one is running and, if so, re-attach: show what has streamed
   * so far, keep streaming, then re-read the thread once it lands. */
  async function catchUp(clientId: number, id: number, cancelled?: () => boolean) {
    let status: api.TurnStatus | null;
    try {
      status = (await api.chatStatus(clientId, id)).turn;
    } catch {
      return; // status is a convenience; the thread loaded fine without it
    }
    const gone = () => cancelled?.() === true || sessionIdRef.current !== id;
    if (gone() || !status) return;
    if (!status.in_flight) {
      // Finished between the thread fetch and this call: the reply is in
      // the transcript now but not on screen if the thread ended on the
      // question — re-read rather than wait for the user to notice.
      setMessages((m) => {
        const last = m[m.length - 1];
        if (last?.from === 'user') void refreshThreadFor(clientId, id);
        return m;
      });
      return;
    }
    const assistantId = idCounter++;
    setMessages((m) => [...m, { id: assistantId, from: 'assistant', text: status!.text }]);
    setStreaming(true);
    try {
      for await (const chunk of api.resumeChat(clientId, id, status.chunk_count)) {
        if (gone()) return;
        setMessages((m) => m.map((msg) => (msg.id === assistantId ? { ...msg, text: msg.text + chunk } : msg)));
      }
    } catch {
      /* the answer still lands in the transcript; the refresh below shows it */
    } finally {
      if (!gone()) {
        setStreaming(false);
        void refreshSessions();
        void refreshThreadFor(clientId, id);
      }
    }
  }

  async function refreshThreadFor(clientId: number, id: number) {
    try {
      const opened = await api.getClientMessages(clientId, id);
      if (opened.messages.length) setMessages(toMessages(opened.messages, activeClient?.name ?? ''));
    } catch {
      /* keep what's on screen — it's the same text, just without ids */
    }
  }

  // Switching clients loads their conversations; opening one loads its
  // messages. Both the list and the thread are per client, and the agent
  // behind them is per conversation, so nothing carries across either line.
  useEffect(() => {
    if (!activeClient) {
      setSessions([]);
      setSessionId(null);
      setMessages([]);
      return;
    }
    let cancelled = false;
    setLoadingHistory(true);
    setMessages([]);
    setRatings({});
    Promise.all([api.listSessions(activeClient.id), api.getClientMessages(activeClient.id)])
      .then(([list, opened]) => {
        if (cancelled) return;
        setSessions(list);
        setSessionId(opened.session_id);
        setMessages(toMessages(opened.messages, activeClient.name));
        void loadRatings(opened.session_id, () => cancelled);
        void catchUp(activeClient.id, opened.session_id, () => cancelled);
      })
      .catch(() => {
        if (!cancelled) setMessages([greeting(activeClient.name)]);
      })
      .finally(() => {
        if (!cancelled) setLoadingHistory(false);
      });
    return () => {
      cancelled = true; // a fast switch must not let the slower fetch win
    };
  }, [activeClient]);

  async function openSession(id: number) {
    if (!activeClient || id === sessionId) return;
    setLoadingHistory(true);
    setMessages([]);
    try {
      const opened = await api.getClientMessages(activeClient.id, id);
      setSessionId(opened.session_id);
      setMessages(toMessages(opened.messages, activeClient.name));
      setRatings({});
      void loadRatings(opened.session_id);
      void catchUp(activeClient.id, opened.session_id);
    } finally {
      setLoadingHistory(false);
      setPickerOpen(false);
    }
  }

  async function newConversation() {
    if (!activeClient) return;
    const created = await api.createSession(activeClient.id);
    setSessions((prev) => [created, ...prev]);
    setSessionId(created.id);
    setMessages([greeting(activeClient.name)]);
    setRatings({});
    setPickerOpen(false);
  }

  async function deleteConversation(id: number) {
    if (!activeClient) return;
    await api.deleteSession(activeClient.id, id);
    const remaining = sessions.filter((s) => s.id !== id);
    setSessions(remaining);
    if (id === sessionId) {
      if (remaining.length) await openSession(remaining[0].id);
      else await newConversation();
    }
  }

  async function refreshSessions() {
    if (!activeClient) return;
    try {
      setSessions(await api.listSessions(activeClient.id));
    } catch {
      /* the list is a convenience; a failed refresh shouldn't break the chat */
    }
  }

  /** The SSE stream carries text, not row ids, so a just-streamed answer has
   * no `serverId` to hang a thumb on. Re-reading the thread once the turn
   * lands is the cheap way to get them — same content, now with ids. */
  async function refreshThread() {
    if (!activeClient || sessionId == null) return;
    try {
      const opened = await api.getClientMessages(activeClient.id, sessionId);
      if (opened.messages.length) setMessages(toMessages(opened.messages, activeClient.name));
    } catch {
      /* keep what's on screen — it's the same text, just without ids */
    }
  }

  async function rate(messageId: number, rating: number, body?: string) {
    // Optimistic: the thumb lights immediately and rolls back if the write
    // fails, because a button that waits on a round trip feels broken.
    const previous = ratings[messageId];
    setRatings((r) => ({ ...r, [messageId]: rating }));
    try {
      await api.sendFeedback({ session_id: sessionId!, message_id: messageId, rating, body });
    } catch {
      setRatings((r) => {
        const next = { ...r };
        if (previous === undefined) delete next[messageId];
        else next[messageId] = previous;
        return next;
      });
    }
  }

  async function clearRating(messageId: number) {
    const previous = ratings[messageId];
    setRatings((r) => {
      const next = { ...r };
      delete next[messageId];
      return next;
    });
    if (commentFor === messageId) setCommentFor(null);
    try {
      await api.clearFeedback(messageId);
    } catch {
      if (previous !== undefined) setRatings((r) => ({ ...r, [messageId]: previous }));
    }
  }

  function onRate(messageId: number, rating: number) {
    void rate(messageId, rating);
    // Only a thumb-down asks why. A thumb-up needs no explanation.
    if (rating === -1) {
      setCommentFor(messageId);
      setCommentDraft('');
    } else {
      setCommentFor(null);
    }
  }

  async function submitComment(messageId: number) {
    const body = commentDraft.trim();
    setCommentFor(null);
    setCommentDraft('');
    if (body) await rate(messageId, -1, body);
  }

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || streaming || !llmConfigured || !activeClient || sessionId == null) return;
    setDraft('');
    setMessages((m) => [...m, { id: idCounter++, from: 'user', text: trimmed }]);
    setStreaming(true);
    const assistantId = idCounter++;
    setMessages((m) => [...m, { id: assistantId, from: 'assistant', text: '' }]);
    try {
      for await (const chunk of api.streamChat(trimmed, activeClient.id, sessionId)) {
        setMessages((m) => m.map((msg) => (msg.id === assistantId ? { ...msg, text: msg.text + chunk } : msg)));
      }
    } catch (err) {
      // 409: the server is still answering an earlier message in this
      // conversation (another tab, or a send that raced a reload). Show
      // that answer instead of a connection error — the message just typed
      // was not accepted and stays in the box to resend afterwards.
      if (err instanceof api.ApiError && err.status === 409) {
        setMessages((m) => m.filter((msg) => msg.id !== assistantId && !(msg.from === 'user' && msg.text === trimmed && msg.serverId == null)));
        setDraft(trimmed);
        setStreaming(false);
        void catchUp(activeClient.id, sessionId);
        return;
      }
      setMessages((m) =>
        m.map((msg) => (msg.id === assistantId ? { ...msg, text: msg.text || "Couldn't reach Aboyeur — check your connection and try again." } : msg)),
      );
    } finally {
      setStreaming(false);
      // Titles are set from the first message server-side, and the list is
      // ordered by last activity — so it's stale the moment a turn lands.
      void refreshSessions();
      void refreshThread();
    }
  }

  function onComposerKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void send(draft);
    }
  }

  return (
    <div className="chat-app">
      {activeClient && (
        <div className="chat-client-bar">
          <div className="chat-session-picker">
            <button type="button" className="chat-session-current" onClick={() => setPickerOpen((v) => !v)}>
              <strong>{sessions.find((s) => s.id === sessionId)?.title ?? 'New conversation'}</strong>
              <span>
                {activeClient.name} · {sessions.length} conversation{sessions.length === 1 ? '' : 's'}
                {loadingHistory ? ' · loading…' : ''}
              </span>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="m6 9 6 6 6-6" />
              </svg>
            </button>

            {pickerOpen && (
              <div className="chat-session-menu">
                {sessions.map((s) => (
                  <div key={s.id} className={`chat-session-row ${s.id === sessionId ? 'chat-session-row-on' : ''}`}>
                    <button type="button" className="chat-session-open" onClick={() => void openSession(s.id)}>
                      <strong>{s.title}</strong>
                      <span>
                        {s.message_count} message{s.message_count === 1 ? '' : 's'}
                        {s.preview ? ` · ${s.preview.slice(0, 60)}` : ''}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="chat-session-delete"
                      title="Delete conversation"
                      onClick={() => void deleteConversation(s.id)}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6" />
                      </svg>
                    </button>
                  </div>
                ))}
                <button type="button" className="chat-session-new" onClick={() => void newConversation()}>
                  + New conversation
                </button>
              </div>
            )}
          </div>

          <div className="chat-client-bar-actions">
            <button type="button" onClick={() => void newConversation()} disabled={streaming}>
              New conversation
            </button>
            <Link to={`/app/clients/${activeClient.id}`}>Open their profile</Link>
          </div>
        </div>
      )}
      <div className="chat-thread" ref={threadRef}>
        {messages.map((m) => (
          <div key={m.id} className={`chat-row ${m.from === 'user' ? 'chat-row-user' : 'chat-row-assist'}`}>
            {m.from === 'user' ? (
              <div className="chat-avatar chat-avatar-user">M</div>
            ) : (
              <Logo size={28} className="chat-avatar" />
            )}
            <div className="chat-bubble-col">
              <div className={`chat-bubble ${m.from === 'user' ? 'chat-bubble-user' : 'chat-bubble-assist'}`}>
                {m.from === 'assistant' ? (
                  m.text ? (
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.text}</ReactMarkdown>
                  ) : (
                    <div className="chat-typing-dots">
                      <span className="chat-typing-dot" />
                      <span className="chat-typing-dot" />
                      <span className="chat-typing-dot" />
                    </div>
                  )
                ) : (
                  m.text
                )}
              </div>

              {/* Rateable only once the turn is persisted and finished — the
                  greeting has no row behind it and a streaming answer isn't
                  done being judged. */}
              {m.from === 'assistant' && m.serverId != null && !streaming && (
                <FeedbackBar
                  rating={ratings[m.serverId]}
                  onRate={(r) => onRate(m.serverId!, r)}
                  onClear={() => void clearRating(m.serverId!)}
                />
              )}

              {m.serverId != null && commentFor === m.serverId && (
                <div className="chat-fb-box">
                  <label htmlFor={`fb-${m.serverId}`}>What went wrong? (optional)</label>
                  <textarea
                    id={`fb-${m.serverId}`}
                    autoFocus
                    rows={2}
                    value={commentDraft}
                    placeholder="Wrong portions, ignored an allergy, made up a recipe…"
                    onChange={(e) => setCommentDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        void submitComment(m.serverId!);
                      }
                      if (e.key === 'Escape') setCommentFor(null);
                    }}
                  />
                  <div className="chat-fb-box-actions">
                    <button type="button" className="chat-fb-send" onClick={() => void submitComment(m.serverId!)}>
                      Send
                    </button>
                    <button type="button" className="chat-fb-skip" onClick={() => setCommentFor(null)}>
                      Skip
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      {!activeClient ? (
        <div className="chat-setup-cta">
          <div>
            <strong>Pick a client to plan for.</strong>
            <p>Every menu, kit and shopping list belongs to one client, so chat needs to know whose it is.</p>
          </div>
          <Link to="/app/clients" className="btn-primary chat-setup-btn">
            Open Clients
          </Link>
        </div>
      ) : !llmConfigured ? (
        // No model configured: the backend would only answer with the same
        // nudge, so ask for setup here instead of letting them type into a
        // dead composer.
        <div className="chat-setup-cta">
          <div>
            <strong>Connect an AI model to start chatting.</strong>
            <p>Aboyeur needs an OpenAI-compatible endpoint and key before it can plan anything.</p>
          </div>
          <Link to="/app/settings" className="btn-primary chat-setup-btn">
            Open Settings
          </Link>
        </div>
      ) : (
        <>
        <div className="chat-suggest-row">
          {SUGGESTIONS.map((s) => (
            <button key={s} type="button" className="chat-suggest-chip" onClick={() => void send(s)} disabled={streaming}>
              {s}
            </button>
          ))}
        </div>

        <div className="chat-composer">
          <textarea
            className="chat-composer-input"
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onComposerKeyDown}
            placeholder={`Message Aboyeur about ${activeClient.name}`}
          />
          <button type="button" className="chat-send-btn" onClick={() => void send(draft)} disabled={!draft.trim() || streaming} aria-label="Send">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
              <path d="M3 11.5 21 3l-6 18-3.5-7.5L3 11.5Z" />
            </svg>
          </button>
        </div>
        </>
      )}
    </div>
  );
}
