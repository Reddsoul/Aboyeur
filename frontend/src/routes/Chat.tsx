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
  id: number;
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
    from: m.role === 'user' ? ('user' as const) : ('assistant' as const),
    text: m.content,
  }));
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
  const threadRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, streaming]);

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
    Promise.all([api.listSessions(activeClient.id), api.getClientMessages(activeClient.id)])
      .then(([list, opened]) => {
        if (cancelled) return;
        setSessions(list);
        setSessionId(opened.session_id);
        setMessages(toMessages(opened.messages, activeClient.name));
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
    } catch {
      setMessages((m) =>
        m.map((msg) => (msg.id === assistantId ? { ...msg, text: msg.text || "Couldn't reach Aboyeur — check your connection and try again." } : msg)),
      );
    } finally {
      setStreaming(false);
      // Titles are set from the first message server-side, and the list is
      // ordered by last activity — so it's stale the moment a turn lands.
      void refreshSessions();
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
