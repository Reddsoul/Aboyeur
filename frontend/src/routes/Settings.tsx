import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import './Settings.css';
import * as api from '../api';
import { ApiError } from '../api';
import { useAuth } from '../auth/AuthContext';
import { ClinicalNote } from '../components/Legal';

const dollars = (cents: number) => `$${(cents / 100).toFixed(cents % 100 === 0 ? 0 : 2)}`;

/** The hosted card's body: balance, the price they'd pay, and top-ups.
 * The provider is shown by display name only — never its endpoint. */
function HostedPanel({ settings, reload }: { settings: api.LlmSettings; reload: () => void }) {
  const [params] = useSearchParams();
  const [busy, setBusy] = useState<number | null>(null);
  const [msg, setMsg] = useState<string | null>(
    params.get('topup') === 'success'
      ? 'Payment received — your balance updates within a few seconds.'
      : params.get('topup') === 'cancel'
        ? 'Top-up cancelled — nothing was charged.'
        : null,
  );

  useEffect(() => {
    if (params.get('topup') === 'success') {
      const t = setTimeout(reload, 4000);
      return () => clearTimeout(t);
    }
  }, [params, reload]);

  async function topup(cents: number) {
    setBusy(cents);
    setMsg(null);
    try {
      const { url } = await api.billingTopup(cents);
      window.location.assign(url);
    } catch (err) {
      setMsg(err instanceof ApiError ? (err.code === 'unverified' ? 'Verify your email first.' : err.message) : "Couldn't reach Aboyeur.");
      setBusy(null);
    }
  }

  const offer = settings.hosted_offer;
  return (
    <div className="set-hosted">
      <div className="set-hosted-row">
        <span>Credit balance</span>
        <strong style={{ color: settings.credit_cents <= 0 ? 'var(--danger)' : undefined }}>{dollars(settings.credit_cents)}</strong>
      </div>
      {offer ? (
        <div className="set-hint">
          You'll get <strong>{offer.name}</strong>. {offer.price_in_per_ktok_cents.toFixed(3)}¢ per 1K input tokens,{' '}
          {offer.price_out_per_ktok_cents.toFixed(3)}¢ per 1K output tokens, charged per message and rounded up to the cent.
          A typical menu-planning turn is a few cents.
        </div>
      ) : (
        <div className="set-hint">No hosted model is configured on this deploy yet.</div>
      )}
      {!settings.is_paid ? (
        <div className="set-hint">
          Hosted models need Pro.{' '}
          <Link to="/app/account" style={{ color: 'var(--accent)', fontWeight: 600 }}>Upgrade on your Account page</Link>.
        </div>
      ) : settings.billing_status === 'comp' ? (
        <div className="set-hint">Complimentary account — hosted turns aren't charged.</div>
      ) : settings.billing_enabled ? (
        <div className="set-topups">
          {settings.topup_amounts.map((c) => (
            <button key={c} type="button" className="btn-ghost" disabled={busy !== null} onClick={() => void topup(c)}>
              {busy === c ? 'Opening…' : `Add ${dollars(c)}`}
            </button>
          ))}
        </div>
      ) : (
        <div className="set-hint">Billing isn't enabled on this deploy.</div>
      )}
      {msg && <div className="set-hint" style={{ color: 'var(--ink)' }}>{msg}</div>}
      <div className="set-hint">Credits don't expire for 12 months and aren't refundable once used.</div>
    </div>
  );
}

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
  const [settings, setSettings] = useState<api.LlmSettings | null>(null);
  const [mode, setMode] = useState<api.LlmMode>('byok');
  const [modeError, setModeError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .getLlmSettings()
      .then((s) => {
        setSettings(s);
        setMode(s.llm_mode);
        setBaseUrl(s.base_url);
        setModel(s.model);
        setConfigured(s.configured);
      })
      .catch((err) => setError(failure(err, "Couldn't load your current settings")))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function chooseMode(next: api.LlmMode) {
    if (next === mode) return;
    setModeError(null);
    try {
      await api.setLlmMode(next);
      setMode(next);
      await refreshLlmStatus();
      load();
    } catch (err) {
      setModeError(
        err instanceof ApiError && err.code === 'hosted_requires_pro'
          ? 'Hosted models need a Pro plan — upgrade on your Account page.'
          : failure(err, "Couldn't switch"),
      );
    }
  }

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
            <div className="set-modes" role="radiogroup" aria-label="Which model runs your chats">
              <button type="button" role="radio" aria-checked={mode === 'byok'} className={`set-mode${mode === 'byok' ? ' set-mode-on' : ''}`} onClick={() => void chooseMode('byok')}>
                <span className="set-mode-title">Use my own API key</span>
                <span className="set-mode-sub">Any OpenAI-compatible endpoint. You pay your provider directly. Unlimited on Free.</span>
              </button>
              <button type="button" role="radio" aria-checked={mode === 'hosted'} className={`set-mode${mode === 'hosted' ? ' set-mode-on' : ''}`} onClick={() => void chooseMode('hosted')}>
                <span className="set-mode-title">
                  Use Aboyeur's models <span className="set-mode-tag">Pro</span>
                </span>
                <span className="set-mode-sub">No key to manage. Pay per use from a prepaid credit balance.</span>
              </button>
            </div>
            {modeError && (
              <div className="set-result set-result-bad">
                <span className="set-result-dot" />
                <span>{modeError}</span>
              </div>
            )}

            {settings && mode === 'hosted' && <HostedPanel settings={settings} reload={load} />}

            {mode === 'byok' && (
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
                  placeholder={settings?.byok_configured ?? configured ? '•••••••• (leave blank to keep current)' : 'sk-…'}
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
            {mode === 'hosted' && firstRun && (
              <div className="set-actions">
                <button type="button" className="btn-primary" onClick={() => navigate('/app/chat', { replace: true })}>
                  Start cooking
                </button>
              </div>
            )}
            <ClinicalNote />
          </>
        )}
      </form>
    </div>
  );
}
