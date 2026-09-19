import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import './Admin.css';
import * as api from '../../api';
import { ActivityChart, ErrorChart, LatencyChart, ThumbsChart } from './Charts';
import { count, duration, percent } from './format';

function Stat({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: string;
  note?: string;
  tone?: 'muted' | 'danger';
}) {
  return (
    <div className="ad-stat">
      <span className="ad-stat-label">{label}</span>
      <span
        className={`ad-stat-value ${tone === 'danger' ? 'ad-stat-value-danger' : ''} ${
          tone === 'muted' ? 'ad-stat-value-muted' : ''
        }`}
      >
        {value}
      </span>
      {note && <span className="ad-stat-note">{note}</span>}
    </div>
  );
}

export default function Dashboard() {
  const [data, setData] = useState<api.AdminOverview | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .adminOverview()
      .then((o) => !cancelled && setData(o))
      .catch(() => !cancelled && setError("Couldn't load the overview."));
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <div className="ad-page">
        <div className="ad-inner">
          <div className="ad-empty">
            <h2>Overview unavailable</h2>
            <p>{error}</p>
          </div>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="ad-page">
        <div className="ad-inner">
          <p className="ad-sub">Loading…</p>
        </div>
      </div>
    );
  }

  const days = data.daily.map((d) => d.day);
  // An error rate is only meaningful once turns have been recorded; before
  // that the backend sends null and every reading here stays honest about it.
  const rateHigh = data.error_rate_7d != null && data.error_rate_7d >= 0.05;

  return (
    <div className="ad-page">
      <div className="ad-inner">
        <div className="ad-head">
          <div>
            <h1 className="ad-title">Dashboard</h1>
            <p className="ad-sub">
              What the deploy has been doing. Counts are last 7 days unless the tile says
              otherwise; charts cover 30.
            </p>
          </div>
        </div>

        {/* Needs attention first: the operator opens this screen to find out
            whether anything is wrong, not to read totals. */}
        <div className="ad-alerts">
          {data.errors_7d > 0 && (
            <Link to="/admin/activity?errors=1" className="ad-alert ad-alert-danger">
              <span className="ad-alert-dot" />
              <span className="ad-alert-main">
                <strong>
                  {data.errors_7d} error{data.errors_7d === 1 ? '' : 's'} in the last 7 days
                </strong>
                <span>
                  {percent(data.error_rate_7d)} of {count(data.turns_7d)} chat turns failed
                </span>
              </span>
              <span className="ad-alert-go">Open activity →</span>
            </Link>
          )}
          {data.feedback_open > 0 && (
            <Link to="/admin/feedback" className="ad-alert ad-alert-warn">
              <span className="ad-alert-dot" />
              <span className="ad-alert-main">
                <strong>
                  {data.feedback_open} open feedback item{data.feedback_open === 1 ? '' : 's'}
                </strong>
                <span>Rated turns nobody has triaged yet</span>
              </span>
              <span className="ad-alert-go">Open feedback →</span>
            </Link>
          )}
          {data.errors_7d === 0 && data.feedback_open === 0 && (
            <div className="ad-alert ad-alert-ok">
              <span className="ad-alert-dot" />
              <span className="ad-alert-main">
                <strong>Nothing needs attention</strong>
                <span>No errors in the last 7 days, no untriaged feedback.</span>
              </span>
            </div>
          )}
        </div>

        <div className="ad-stats">
          <Stat
            label="Active users"
            value={count(data.users_active_7d)}
            note={`${data.users_active_30d} in 30d · ${data.users_total} total`}
          />
          <Stat label="Messages (24h)" value={count(data.messages_24h)} note={`${count(data.messages_7d)} in 7d`} />
          <Stat label="Chat turns (7d)" value={count(data.turns_7d)} note={`${data.sessions_total} conversations all time`} />
          <Stat
            label="Error rate (7d)"
            value={percent(data.error_rate_7d)}
            note={`${data.errors_7d} error${data.errors_7d === 1 ? '' : 's'}`}
            tone={rateHigh ? 'danger' : data.error_rate_7d == null ? 'muted' : undefined}
          />
          <Stat
            label="Latency p95"
            value={duration(data.latency_p95_ms)}
            note={`p50 ${duration(data.latency_p50_ms)}`}
            tone={data.latency_p95_ms == null ? 'muted' : undefined}
          />
          <Stat label="Clients" value={count(data.clients_total)} note={`${count(data.menus_7d)} menus in 7d`} />
        </div>

        <h2 className="ad-section-title">Billing</h2>
        <div className="ad-stats">
          <Stat
            label="MRR"
            value={`$${(data.billing.mrr_cents / 100).toFixed(0)}`}
            note={`${data.billing.active} active · ${data.billing.comp} comp · ${data.billing.free} free`}
          />
          <Stat
            label="Past due"
            value={String(data.billing.past_due)}
            note={`${data.billing.canceled} canceled`}
            tone={data.billing.past_due ? 'danger' : 'muted'}
          />
          <Stat
            label="Webhooks unprocessed"
            value={String(data.billing.unprocessed_webhooks)}
            note="Billing → Webhooks"
            tone={data.billing.unprocessed_webhooks ? 'danger' : 'muted'}
          />
          <Stat
            label="Credit outstanding"
            value={`$${(data.billing.credit_outstanding_cents / 100).toFixed(2)}`}
            note="prepaid, unspent"
          />
        </div>

        <h2 className="ad-section-title">Hosted models (24h)</h2>
        <div className="ad-stats">
          <Stat
            label="Tokens"
            value={count(data.hosted_1d.tokens_in + data.hosted_1d.tokens_out)}
            note={`${count(data.hosted_1d.tokens_in)} in · ${count(data.hosted_1d.tokens_out)} out · ${data.hosted_1d.turns} turns`}
          />
          <Stat label="Our cost" value={`$${(data.hosted_1d.cost_cents / 100).toFixed(2)}`} note={`7d $${(data.hosted_7d.cost_cents / 100).toFixed(2)}`} />
          <Stat label="Charged" value={`$${(data.hosted_1d.charged_cents / 100).toFixed(2)}`} note={`7d $${(data.hosted_7d.charged_cents / 100).toFixed(2)}`} />
          <Stat
            label="Gross margin"
            value={data.hosted_1d.gross_margin_pct == null ? '—' : `${data.hosted_1d.gross_margin_pct}%`}
            note={data.hosted_7d.gross_margin_pct == null ? 'no hosted turns in 7d' : `7d ${data.hosted_7d.gross_margin_pct}%`}
            tone={data.hosted_1d.gross_margin_pct == null ? 'muted' : undefined}
          />
          <Stat
            label="Estimated usage rows (7d)"
            value={String(data.estimated_rows_7d)}
            note={data.estimated_rows_7d ? 'a provider stopped reporting usage' : 'every turn had real usage'}
            tone={data.estimated_rows_7d ? 'danger' : 'muted'}
          />
        </div>
        {data.hosted_7d.top_users.length > 0 && (
          <div className="ad-card">
            <span className="ad-cell-muted">Top hosted spenders, 7d: </span>
            {data.hosted_7d.top_users.map((u, i) => (
              <span key={u.user_id}>
                {i > 0 && ' · '}
                <Link to={`/admin/users/${u.user_id}`}>{u.email ?? `#${u.user_id}`}</Link>{' '}
                <span className="ad-cell-muted">${(u.charge_cents / 100).toFixed(2)}</span>
              </span>
            ))}
          </div>
        )}

        <h2 className="ad-section-title">Last 30 days</h2>
        <div className="ad-chart-grid">
          <ActivityChart
            title="Activity"
            note={`${count(data.messages_30d)} messages`}
            days={days}
            values={data.daily.map((d) => d.messages)}
            overlay={data.daily.map((d) => d.turns)}
            legend={[
              { color: 'var(--accent)', label: 'messages' },
              { color: 'var(--muted)', label: 'agent turns' },
            ]}
          />
          <ErrorChart
            title="Errors"
            note={data.errors_7d > 0 ? `${data.errors_7d} in 7d` : 'none in 7d'}
            days={days}
            errors={data.daily.map((d) => d.errors)}
            turns={data.daily.map((d) => d.turns)}
          />
          <LatencyChart
            title="Turn latency"
            note="successful turns, 7d"
            p50={data.latency_p50_ms}
            p95={data.latency_p95_ms}
          />
          <ThumbsChart title="Feedback" note="30d" up={data.thumbs_up_30d} down={data.thumbs_down_30d} />
        </div>
      </div>
    </div>
  );
}
