import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import './Admin.css';
import * as api from '../../api';
import { relativeTime } from './format';

const blank: api.ProviderBody = { name: '', base_url: '', model: '', api_key: '', price_in_per_mtok: 0, price_out_per_mtok: 0, markup_pct: 30, enabled: true };

/** Hosted-LLM endpoints. Keys are write-only (last 4 shown). Prices are
 * OUR cost in cents per million tokens; users pay cost × (1 + markup). */
export default function Providers() {
  const [providers, setProviders] = useState<api.LlmProvider[] | null>(null);
  const [form, setForm] = useState<api.ProviderBody>(blank);
  const [editing, setEditing] = useState<number | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [testing, setTesting] = useState<Record<number, string>>({});

  const load = () => api.adminProviders().then(setProviders).catch(() => setMsg("Couldn't load providers."));
  useEffect(() => {
    void load();
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setMsg(null);
    try {
      if (editing != null) {
        const patch: Partial<api.ProviderBody> = { ...form };
        if (!patch.api_key) delete patch.api_key;
        await api.adminUpdateProvider(editing, patch);
      } else {
        await api.adminCreateProvider(form);
      }
      setForm(blank);
      setEditing(null);
      await load();
    } catch (err) {
      setMsg(err instanceof api.ApiError ? err.message : 'Failed.');
    }
  }

  async function test(p: api.LlmProvider) {
    setTesting((t) => ({ ...t, [p.id]: 'testing…' }));
    try {
      const r = await api.adminTestProvider(p.id);
      setTesting((t) => ({ ...t, [p.id]: `${r.ok ? '✓' : '✗'} ${r.latency_ms}ms — ${r.detail}` }));
    } catch (err) {
      setTesting((t) => ({ ...t, [p.id]: err instanceof api.ApiError ? err.message : 'failed' }));
    }
  }

  const num = (k: keyof api.ProviderBody) => (e: React.ChangeEvent<HTMLInputElement>) => setForm((f) => ({ ...f, [k]: Number(e.target.value) }));

  return (
    <div className="ad-page">
      <div className="ad-inner">
        <div className="ad-head">
          <div>
            <h1 className="ad-title">Hosted models</h1>
            <p className="ad-sub">
              OpenAI-compatible endpoints Aboyeur pays for and re-bills. The default serves every
              hosted user without an override. Users see the <b>name</b>, never the endpoint.
            </p>
          </div>
        </div>

        {!providers ? (
          <p className="ad-sub">Loading…</p>
        ) : providers.length === 0 ? (
          <div className="ad-empty">
            <h2>No providers yet</h2>
            <p>Add one below. The first becomes the default.</p>
          </div>
        ) : (
          <div className="ad-card ad-card-pad0">
            <div className="ad-table-wrap">
              <table className="ad-table">
                <thead>
                  <tr>
                    <th>Provider</th>
                    <th>Model</th>
                    <th>Key</th>
                    <th className="ad-num">Cost in/out ¢/Mtok</th>
                    <th className="ad-num">Markup</th>
                    <th className="ad-num">User pays ¢/Mtok</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {providers.map((p) => (
                    <tr key={p.id}>
                      <td className="ad-cell-strong">
                        {p.name}
                        {p.is_default && <span className="ad-tag ad-tag-accent" style={{ marginLeft: 8 }}>default</span>}
                        <div className="ad-cell-muted ad-code">{p.base_url}</div>
                      </td>
                      <td className="ad-code">{p.model}</td>
                      <td className="ad-cell-muted ad-code">…{p.key_last4}</td>
                      <td className="ad-num">{p.price_in_per_mtok} / {p.price_out_per_mtok}</td>
                      <td className="ad-num">{p.markup_pct}%</td>
                      <td className="ad-num">{p.user_price_in_per_mtok} / {p.user_price_out_per_mtok}</td>
                      <td>
                        <span className={`ad-tag ${p.enabled ? 'ad-tag-success' : 'ad-tag-danger'}`}>{p.enabled ? 'enabled' : 'disabled'}</span>
                        <div className="ad-cell-muted">{testing[p.id] ?? `updated ${relativeTime(p.updated_at)}`}</div>
                      </td>
                      <td>
                        <div className="ad-inline-form">
                          <button type="button" className="btn-ghost" onClick={() => void test(p)}>Test</button>
                          {!p.is_default && p.enabled && (
                            <button type="button" className="btn-ghost" onClick={() => void api.adminDefaultProvider(p.id).then(load)}>Make default</button>
                          )}
                          <button type="button" className="btn-ghost" onClick={() => void api.adminUpdateProvider(p.id, { enabled: !p.enabled }).then(load).catch((e) => setMsg(e.message))}>
                            {p.enabled ? 'Disable' : 'Enable'}
                          </button>
                          <button
                            type="button"
                            className="btn-ghost"
                            onClick={() => {
                              setEditing(p.id);
                              setForm({ name: p.name, base_url: p.base_url, model: p.model, api_key: '', price_in_per_mtok: p.price_in_per_mtok, price_out_per_mtok: p.price_out_per_mtok, markup_pct: p.markup_pct, enabled: p.enabled });
                            }}
                          >
                            Edit
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <form className="ad-card" onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <h2 className="ad-section-title" style={{ margin: 0 }}>{editing != null ? `Edit provider #${editing}` : 'Add provider'}</h2>
          <div className="ad-inline-form">
            <input className="text-input" required placeholder="Display name (users see this)" value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} style={{ width: 240 }} />
            <input className="text-input" required placeholder="https://api.openai.com/v1" value={form.base_url} onChange={(e) => setForm((f) => ({ ...f, base_url: e.target.value }))} style={{ width: 300 }} />
            <input className="text-input" required placeholder="model id" value={form.model} onChange={(e) => setForm((f) => ({ ...f, model: e.target.value }))} style={{ width: 200 }} />
          </div>
          <div className="ad-inline-form">
            <input className="text-input" type="password" autoComplete="off" placeholder={editing != null ? 'API key (blank = keep)' : 'API key'} value={form.api_key ?? ''} onChange={(e) => setForm((f) => ({ ...f, api_key: e.target.value }))} style={{ width: 300 }} />
            <span className="ad-cell-muted">cost ¢/Mtok in</span>
            <input className="text-input ad-input-sm" type="number" min={0} value={form.price_in_per_mtok} onChange={num('price_in_per_mtok')} />
            <span className="ad-cell-muted">out</span>
            <input className="text-input ad-input-sm" type="number" min={0} value={form.price_out_per_mtok} onChange={num('price_out_per_mtok')} />
            <span className="ad-cell-muted">markup %</span>
            <input className="text-input ad-input-sm" type="number" min={0} value={form.markup_pct} onChange={num('markup_pct')} />
            <button type="submit" className="btn-primary">{editing != null ? 'Save' : 'Add'}</button>
            {editing != null && (
              <button type="button" className="btn-ghost" onClick={() => { setEditing(null); setForm(blank); }}>Cancel</button>
            )}
          </div>
          <p className="ad-caveat">
            Example: gpt-4.1-mini is $0.40/M in, $1.60/M out → 40 / 160. Anthropic's compat endpoint is
            https://api.anthropic.com/v1/. A self-hosted LiteLLM with no auth: any key works.
          </p>
          {msg && <p className="ad-sub">{msg}</p>}
        </form>
      </div>
    </div>
  );
}
