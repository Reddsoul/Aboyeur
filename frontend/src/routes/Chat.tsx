import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './Chat.css';
import * as api from '../api';

interface Message {
  id: number;
  from: 'user' | 'assistant';
  text: string;
}

const SUGGESTIONS = ['Plan my week', 'Find a quick high-protein dinner', 'What can you help me with?'];

let idCounter = 1;

export default function Chat() {
  const [messages, setMessages] = useState<Message[]>([
    { id: idCounter++, from: 'assistant', text: "Hey! I'm Aboyeur. Ask me to plan a menu, search your recipe library, or update your profile — I'll take it from there." },
  ]);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const threadRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, streaming]);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || streaming) return;
    setDraft('');
    setMessages((m) => [...m, { id: idCounter++, from: 'user', text: trimmed }]);
    setStreaming(true);
    const assistantId = idCounter++;
    setMessages((m) => [...m, { id: assistantId, from: 'assistant', text: '' }]);
    try {
      for await (const chunk of api.streamChat(trimmed)) {
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
            <div className={`chat-avatar ${m.from === 'user' ? 'chat-avatar-user' : 'chat-avatar-assist'}`}>{m.from === 'user' ? 'M' : 'A'}</div>
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
          placeholder="Message Aboyeur"
        />
        <button type="button" className="chat-send-btn" onClick={() => void send(draft)} disabled={!draft.trim() || streaming} aria-label="Send">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M3 11.5 21 3l-6 18-3.5-7.5L3 11.5Z" />
          </svg>
        </button>
      </div>
    </div>
  );
}
