import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import './Settings.css';
import * as api from '../api';
import { ApiError } from '../api';
import { useAuth } from '../auth/AuthContext';

interface Props {
  /** First-run variant: full-screen, no app chrome, continues to chat on save. */
  firstRun?: boolean;
}

type TestState = { status: 'idle' } | { status: 'testing' } | { status: 'done'; ok: boolean; detail: string };

/** Distinguishes "the browser never reached Aboyeur" from "Aboyeur said no" —
 * a 401 here means the session cookie isn't sticking, which looks identical to
 * a dead network unless we say so. */
function failure(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return 'Your session expired — sign in again.';
    return `${fallback} (server said ${err.status}: ${err.message})`;
  }
  return `${fallback} — couldn't reach the Aboyeur server.`;
}

export default function Settings({ firstRun = false }: Props) {
  const navigate = useNavigate();
  const { refreshLlmStatus } = useAuth();

  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [configured, setConfigured] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [test, setTest] = useState<TestState>({ status: 'idle' });

  useEffect(() => {
    api
      .getLlmSettings()
      .then((s) => {
        setBaseUrl(s.base_url);
        setModel(s.model);
        setConfigured(s.configured);
      })
      .catch((err) => setError(failure(err, "Couldn't load your current settings")))
      .finally(() => setLoading(false));
  }, []);

  // Any edit invalidates a previous green check — it was about the old values.
  function edited<T>(setter: (v: T) => void) {
    return (value: T) => {
      setter(value);
      setTest({ status: 'idle' });
      setSaved(false);
    };
  }

  async function onTest() {
    setTest({ status: 'testing' });
    setError(null);
    try {
      const res = await api.testLlmConnection({
        base_url: baseUrl,
        model,
        // Blank means "test the key already stored", which is what the user
        // expects when they only changed the model.
        api_key: apiKey || undefined,
      });
      setTest({ status: 'done', ok: res.ok, detail: res.detail });
    } catch (err) {
      setTest({ status: 'done', ok: false, detail: failure(err, "Couldn't run the test") });
    }
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await api.saveLlmSettings({ base_url: baseUrl, model, api_key: apiKey || undefined });
      await refreshLlmStatus();
      setConfigured(true);
      setApiKey('');
      setSaved(true);
      if (firstRun) navigate('/app/chat', { replace: true });
    } catch (err) {
      setError(failure(err, "Couldn't save those settings"));
    } finally {
      setSaving(false);
    }
  }

  const canSubmit = Boolean(baseUrl.trim() && model.trim()) && !saving;

  return (
    <div className={`set-stage${firstRun ? ' set-stage-firstrun' : ''}`}>
      <div
        className="set-orb"
        style={{ top: -150, left: -110, width: 460, height: 460, opacity: 0.14, background: 'linear-gradient(155deg, var(--accent), var(--success))' }}
      />
      <form className="set-card" onSubmit={onSubmit}>
        <div>
          <h1 className="set-title">{firstRun ? 'Connect your AI model' : 'AI model'}</h1>
          <p className="set-sub">
            {firstRun
              ? 'Aboyeur runs on the language model you point it at. Paste an OpenAI-compatible endpoint and key — they stay on the Aboyeur server, encrypted.'
              : 'The OpenAI-compatible endpoint Aboyeur uses for your chats, menus, and nutrition parsing.'}
          </p>
        </div>

        {loading ? (
          <p className="set-sub">Loading…</p>
        ) : (
          <>
            <div className="set-field">
              <label className="set-label" htmlFor="set-base-url">
                Provider base URL
              </label>
              <input
                id="set-base-url"
                className="text-input"
                type="url"
                required
                autoFocus={firstRun}
                value={baseUrl}
                onChange={(e) => edited(setBaseUrl)(e.target.value)}
                placeholder="https://api.openai.com/v1"
              />
              <p className="set-hint">Must end in /v1 for most providers. Self-hosted works too.</p>
            </div>

            <div className="set-field">
              <label className="set-label" htmlFor="set-model">
                Model
              </label>
              <input
                id="set-model"
                className="text-input"
                required
                value={model}
                onChange={(e) => edited(setModel)(e.target.value)}
                placeholder="gpt-4o-mini"
              />
            </div>

            <div className="set-field">
              <label className="set-label" htmlFor="set-api-key">
                API key
              </label>
              <div className="set-key-row">
                <input
                  id="set-api-key"
                  className="text-input"
                  type={showKey ? 'text' : 'password'}
                  autoComplete="off"
                  value={apiKey}
                  onChange={(e) => edited(setApiKey)(e.target.value)}
                  placeholder={configured ? '•••••••• (leave blank to keep current)' : 'sk-…'}
                />
                {apiKey && (
                  <button type="button" className="set-key-reveal" onClick={() => setShowKey((v) => !v)}>
                    {showKey ? 'Hide' : 'Show'}
                  </button>
                )}
              </div>
              <p className="set-hint">Stored encrypted on the server and never sent back to this browser.</p>
            </div>

            {test.status === 'testing' && (
              <div className="set-result">
                <span className="set-spinner" />
                Testing the connection…
              </div>
            )}
            {test.status === 'done' && (
              <div className={`set-result ${test.ok ? 'set-result-ok' : 'set-result-bad'}`}>
                <span className="set-result-dot" />
                <span>{test.detail}</span>
              </div>
            )}
            {error && (
              <div className="set-result set-result-bad">
                <span className="set-result-dot" />
                <span>{error}</span>
              </div>
            )}
            {saved && !firstRun && (
              <div className="set-result set-result-ok">
                <span className="set-result-dot" />
                <span>Saved. Your next message uses this model.</span>
              </div>
            )}

            <div className="set-actions">
              <button type="submit" className="btn-primary" disabled={!canSubmit}>
                {saving ? 'Saving…' : firstRun ? 'Save and start cooking' : 'Save'}
              </button>
              <button
                type="button"
                className="btn-ghost"
                onClick={() => void onTest()}
                disabled={!baseUrl.trim() || !model.trim() || test.status === 'testing'}
              >
                Test connection
              </button>
              {firstRun && (
                <button type="button" className="btn-ghost" onClick={() => navigate('/app/chat', { replace: true })}>
                  Skip for now
                </button>
              )}
            </div>

            <p className="set-note">
              Aboyeur makes the model call server-side, so private endpoints on your own network work without exposing
              them to the browser.
            </p>
          </>
        )}
      </form>
    </div>
  );
}
