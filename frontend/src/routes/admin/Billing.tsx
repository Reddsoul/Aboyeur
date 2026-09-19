import { useEffect, useState } from 'react';
import './Admin.css';
import * as api from '../../api';
import { COUNTRIES, countryName } from '../../countries';
import { dateTime, relativeTime } from './format';

const dollars = (cents: number) => `$${(cents / 100).toFixed(2)}`;

/** plan_prices is the source of truth; each active row maps to one Stripe
 * Price. Editing archives the row + Price and creates new ones. Existing
 * subscribers keep the Price their subscription was created with. */
function Prices() {
  const [prices, setPrices] = useState<api.PlanPrice[] | null>(null);
  const [edit, setEdit] = useState<Record<number, string>>({});
  const [add, setAdd] = useState<{ interval: 'month' | 'year'; country: string; amount: string }>({ interval: 'month', country: 'US', amount: '' });
  const [msg, setMsg] = useState<string | null>(null);

  const load = () => api.adminPrices().then(setPrices).catch(() => setMsg("Couldn't load prices."));
  useEffect(() => {
    void load();
  }, []);

  async function save(p: api.PlanPrice) {
    const raw = edit[p.id];
    const cents = Math.round(Number(raw) * 100);
    if (!Number.isFinite(cents) || cents < 0) return;
    setMsg(null);
    try {
      await api.adminUpdatePrice(p.id, cents);
      setEdit((e) => {
        const next = { ...e };
        delete next[p.id];
        return next;
      });
      await load();
      setMsg('Saved. Old Stripe Price archived; existing subscribers are not repriced.');
    } catch (err) {
      setMsg(err instanceof api.ApiError ? err.message : 'Failed.');
    }
  }

  async function create() {
    const cents = Math.round(Number(add.amount) * 100);
    if (!Number.isFinite(cents) || cents < 0) return;
    setMsg(null);
    try {
      await api.adminCreatePrice({ interval: add.interval, country: add.country, amount_cents: cents });
      setAdd((a) => ({ ...a, amount: '' }));
      await load();
    } catch (err) {
      setMsg(err instanceof api.ApiError ? err.message : 'Failed.');
    }
  }

  async function remove(p: api.PlanPrice) {
    if (!window.confirm(`Remove the ${countryName(p.country)} ${p.interval} price? Users there fall back to the default.`)) return;
    try {
      await api.adminDeletePrice(p.id);
      await load();
    } catch (err) {
      setMsg(err instanceof api.ApiError ? err.message : 'Failed.');
    }
  }

  const grouped = (prices ?? []).reduce<Record<string, api.PlanPrice[]>>((acc, p) => {
    (acc[`${p.plan}/${p.interval}`] ??= []).push(p);
    return acc;
  }, {});

  return (
    <>
      <div className="ad-caveat">
        Prices are USD. Each row becomes a Stripe Price on first checkout. Editing a row archives it
        and makes a new one — <b>subscribers already on a price keep it</b>; the new amount applies to
        new checkouts only.
      </div>
      {!prices ? (
        <p className="ad-sub">Loading…</p>
      ) : (
        Object.entries(grouped).map(([key, rows]) => (
          <div className="ad-card ad-card-pad0" key={key}>
            <div className="ad-table-wrap">
              <table className="ad-table">
                <thead>
                  <tr>
                    <th>{key}</th>
                    <th>Country</th>
                    <th className="ad-num">Amount</th>
                    <th>Stripe</th>
                    <th>Updated</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((p) => (
                    <tr key={p.id}>
                      <td className="ad-cell-muted">#{p.id}</td>
                      <td>
                        {countryName(p.country)} {p.country === '*' && <span className="ad-tag">default</span>}
                      </td>
                      <td className="ad-num">
                        <div className="ad-inline-form" style={{ justifyContent: 'flex-end' }}>
                          <input
                            className="text-input ad-input-sm"
                            value={edit[p.id] ?? (p.amount_cents / 100).toFixed(2)}
                            onChange={(e) => setEdit((x) => ({ ...x, [p.id]: e.target.value }))}
                          />
                          {edit[p.id] != null && (
                            <button type="button" className="btn-primary" onClick={() => void save(p)}>
                              Save
                            </button>
                          )}
                        </div>
                      </td>
                      <td className="ad-cell-muted ad-code">{p.stripe_price_id ?? 'not yet'}</td>
                      <td className="ad-cell-muted" title={dateTime(p.updated_at)}>{relativeTime(p.updated_at)}</td>
                      <td>
                        {p.country !== '*' && (
                          <button type="button" className="btn-ghost" style={{ height: 28, fontSize: 12 }} onClick={() => void remove(p)}>
                            Remove
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))
      )}
      <div className="ad-card">
        <div className="ad-inline-form">
          <span className="ad-cell-muted">Add country price</span>
          <select className="text-input" value={add.interval} onChange={(e) => setAdd((a) => ({ ...a, interval: e.target.value as 'month' | 'year' }))}>
            <option value="month">month</option>
            <option value="year">year</option>
          </select>
          <select className="text-input" value={add.country} onChange={(e) => setAdd((a) => ({ ...a, country: e.target.value }))}>
            {COUNTRIES.map(([c, n]) => (
              <option key={c} value={c}>{n}</option>
            ))}
          </select>
          <input className="text-input ad-input-sm" placeholder="USD" value={add.amount} onChange={(e) => setAdd((a) => ({ ...a, amount: e.target.value }))} />
          <button type="button" className="btn-primary" disabled={!add.amount} onClick={() => void create()}>
            Add
          </button>
        </div>
      </div>
      {msg && <p className="ad-sub">{msg}</p>}
    </>
  );
}

/** Tenure ladder. Percent edits drop the tier's Stripe coupon; it's
 * re-created and re-applied per user on their next invoice, never in bulk. */
function Loyalty() {
  const [ladder, setLadder] = useState<api.LoyaltyTier[] | null>(null);
  const [draft, setDraft] = useState<Record<string, { min: string; pct: string; active: boolean }>>({});
  const [newTier, setNewTier] = useState({ tier: '', min: '', pct: '' });
  const [msg, setMsg] = useState<string | null>(null);

  const load = () =>
    api.adminLoyalty().then((l) => {
      setLadder(l);
      setDraft(Object.fromEntries(l.map((t) => [t.tier, { min: String(t.min_paid_months), pct: String(t.percent_off), active: !!t.active }])));
    });
  useEffect(() => {
    void load().catch(() => setMsg("Couldn't load the ladder."));
  }, []);

  async function save(tier: string, d: { min: string; pct: string; active: boolean }) {
    setMsg(null);
    try {
      await api.adminUpsertTier(tier, { min_paid_months: Number(d.min), percent_off: Number(d.pct), active: d.active });
      await load();
      setMsg('Saved. A changed percent applies to each subscriber on their next paid invoice.');
    } catch (err) {
      setMsg(err instanceof api.ApiError ? err.message : 'Failed.');
    }
  }

  return (
    <>
      <div className="ad-caveat">
        After <b>min months</b> of consecutive paid tenure the subscription gets a forever coupon of
        <b> % off</b>. Cancelling or lapsing resets tenure to zero. Stripe coupons are immutable, so
        editing a percent archives the coupon and re-applies a new one lazily.
      </div>
      {!ladder ? (
        <p className="ad-sub">Loading…</p>
      ) : (
        <div className="ad-card ad-card-pad0">
          <div className="ad-table-wrap">
            <table className="ad-table">
              <thead>
                <tr>
                  <th>Tier</th>
                  <th className="ad-num">Min months</th>
                  <th className="ad-num">% off</th>
                  <th>Active</th>
                  <th>Coupon</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {ladder.map((t) => {
                  const d = draft[t.tier] ?? { min: '', pct: '', active: true };
                  return (
                    <tr key={t.tier}>
                      <td className="ad-cell-strong">{t.tier}</td>
                      <td className="ad-num">
                        <input className="text-input ad-input-sm" value={d.min} onChange={(e) => setDraft((x) => ({ ...x, [t.tier]: { ...d, min: e.target.value } }))} />
                      </td>
                      <td className="ad-num">
                        <input className="text-input ad-input-sm" value={d.pct} onChange={(e) => setDraft((x) => ({ ...x, [t.tier]: { ...d, pct: e.target.value } }))} />
                      </td>
                      <td>
                        <input type="checkbox" checked={d.active} onChange={(e) => setDraft((x) => ({ ...x, [t.tier]: { ...d, active: e.target.checked } }))} />
                      </td>
                      <td className="ad-cell-muted ad-code">{t.stripe_coupon_id ?? 'not yet'}</td>
                      <td>
                        <button type="button" className="btn-ghost" style={{ height: 28, fontSize: 12 }} onClick={() => void save(t.tier, d)}>
                          Save
                        </button>
                      </td>
                    </tr>
                  );
                })}
                <tr>
                  <td>
                    <input className="text-input ad-input-sm" placeholder="m24" value={newTier.tier} onChange={(e) => setNewTier((n) => ({ ...n, tier: e.target.value }))} />
                  </td>
                  <td className="ad-num">
                    <input className="text-input ad-input-sm" placeholder="24" value={newTier.min} onChange={(e) => setNewTier((n) => ({ ...n, min: e.target.value }))} />
                  </td>
                  <td className="ad-num">
                    <input className="text-input ad-input-sm" placeholder="30" value={newTier.pct} onChange={(e) => setNewTier((n) => ({ ...n, pct: e.target.value }))} />
                  </td>
                  <td colSpan={2} />
                  <td>
                    <button
                      type="button"
                      className="btn-primary"
                      style={{ height: 28, fontSize: 12 }}
                      disabled={!newTier.tier || !newTier.min || !newTier.pct}
                      onClick={() => void save(newTier.tier, { min: newTier.min, pct: newTier.pct, active: true }).then(() => setNewTier({ tier: '', min: '', pct: '' }))}
                    >
                      Add tier
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      )}
      {msg && <p className="ad-sub">{msg}</p>}
    </>
  );
}

function Webhooks() {
  const [events, setEvents] = useState<api.StripeEventRow[] | null>(null);
  useEffect(() => {
    api.adminStripeEvents().then(setEvents).catch(() => setEvents([]));
  }, []);
  if (!events) return <p className="ad-sub">Loading…</p>;
  if (events.length === 0) return <p className="ad-sub">Every Stripe event so far was processed.</p>;
  return (
    <div className="ad-card ad-card-pad0">
      <div className="ad-table-wrap">
        <table className="ad-table">
          <thead>
            <tr>
              <th>Event</th>
              <th>Type</th>
              <th>Received</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.id}>
                <td className="ad-code">{e.id}</td>
                <td>{e.type}</td>
                <td className="ad-cell-muted" title={dateTime(e.received_at)}>{relativeTime(e.received_at)}</td>
                <td className="ad-cell-muted">{e.error ?? 'pending'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Audit() {
  const [rows, setRows] = useState<api.AuditRow[] | null>(null);
  useEffect(() => {
    api.adminAudit(100).then(setRows).catch(() => setRows([]));
  }, []);
  if (!rows) return <p className="ad-sub">Loading…</p>;
  if (rows.length === 0) return <p className="ad-sub">Nothing audited yet.</p>;
  return (
    <div className="ad-card ad-card-pad0">
      <div className="ad-table-wrap">
        <table className="ad-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Admin</th>
              <th>Action</th>
              <th>Target</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="ad-cell-muted" title={dateTime(r.ts)}>{relativeTime(r.ts)}</td>
                <td>{r.admin_email ?? `#${r.admin_id}`}</td>
                <td><span className={`ad-tag ${r.action === 'reveal_transcript' ? 'ad-tag-danger' : ''}`}>{r.action}</span></td>
                <td className="ad-code">{r.target}</td>
                <td className="ad-cell-muted ad-code">{r.meta_json}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function Billing() {
  const [tab, setTab] = useState<'prices' | 'loyalty' | 'webhooks' | 'audit'>('prices');
  const [health, setHealth] = useState<api.BillingHealth | null>(null);
  useEffect(() => {
    api.adminOverview().then((o) => setHealth(o.billing)).catch(() => setHealth(null));
  }, []);
  const tabBtn = (id: typeof tab, label: string) => (
    <button type="button" className={`ad-tab ${tab === id ? 'ad-tab-on' : ''}`} onClick={() => setTab(id)}>
      {label}
    </button>
  );
  return (
    <div className="ad-page">
      <div className="ad-inner">
        <div className="ad-head">
          <div>
            <h1 className="ad-title">Billing</h1>
            <p className="ad-sub">
              Prices and the loyalty ladder live here, Stripe mirrors them. Cards, invoices and dunning
              live in Stripe's dashboard.
            </p>
          </div>
        </div>
        {health && (
          <div className="ad-stats">
            <div className="ad-stat"><span className="ad-stat-label">MRR</span><span className="ad-stat-value">{dollars(health.mrr_cents)}</span></div>
            <div className="ad-stat"><span className="ad-stat-label">Active</span><span className="ad-stat-value">{health.active}</span></div>
            <div className="ad-stat"><span className="ad-stat-label">Past due</span><span className={`ad-stat-value ${health.past_due ? 'ad-stat-value-danger' : 'ad-stat-value-muted'}`}>{health.past_due}</span></div>
            <div className="ad-stat"><span className="ad-stat-label">Comp</span><span className="ad-stat-value">{health.comp}</span></div>
            <div className="ad-stat"><span className="ad-stat-label">Credit outstanding</span><span className="ad-stat-value">{dollars(health.credit_outstanding_cents)}</span></div>
            <div className="ad-stat"><span className="ad-stat-label">Unprocessed webhooks</span><span className={`ad-stat-value ${health.unprocessed_webhooks ? 'ad-stat-value-danger' : 'ad-stat-value-muted'}`}>{health.unprocessed_webhooks}</span></div>
          </div>
        )}
        <div className="ad-nav" style={{ alignSelf: 'flex-start', marginLeft: 0 }}>
          {tabBtn('prices', 'Prices')}
          {tabBtn('loyalty', 'Loyalty')}
          {tabBtn('webhooks', 'Webhooks')}
          {tabBtn('audit', 'Audit log')}
        </div>
        {tab === 'prices' && <Prices />}
        {tab === 'loyalty' && <Loyalty />}
        {tab === 'webhooks' && <Webhooks />}
        {tab === 'audit' && <Audit />}
      </div>
    </div>
  );
}
