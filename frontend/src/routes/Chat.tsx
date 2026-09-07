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

export default function Chat() {
  const { llmConfigured } = useAuth();
  const { activeClient } = useClients();
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const threadRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, streaming]);

  // Switching clients starts a new conversation on screen, matching the
  // server, which hands out a separate agent per client. Carrying the old
  // transcript over would show one client's plan under another's name.
  useEffect(() => {
    if (!activeClient) {
      setMessages([]);
      return;
    }
    setMessages([
      {
        id: idCounter++,
        from: 'assistant',
        text: `Planning for **${activeClient.name}**. Ask for a week of dinners, a macro kit, or a swap — I'll use their profile, targets and allergies only.`,
      },
    ]);
  }, [activeClient]);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || streaming || !llmConfigured || !activeClient) return;
    setDraft('');
    setMessages((m) => [...m, { id: idCounter++, from: 'user', text: trimmed }]);
    setStreaming(true);
    const assistantId = idCounter++;
    setMessages((m) => [...m, { id: assistantId, from: 'assistant', text: '' }]);
    try {
      for await (const chunk of api.streamChat(trimmed, activeClient.id)) {
        setMessages((m) => m.map((msg) => (msg.id === assistantId ? { ...msg, text: msg.text + chunk } : msg)));
      }
    } catch {
      setMessages((m) =>
        m.map((msg) => (msg.id === assistantId ? { ...msg, text: msg.text || "Couldn't reach Aboyeur — check your connection and try again." } : msg)),
      );
    } finally {
      setStreaming(false);
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
