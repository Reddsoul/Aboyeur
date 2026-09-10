/** Charts for the admin dashboard, hand-rolled in SVG.
 *
 * No chart library on purpose. The frontend has five runtime dependencies and
 * a deliberately quiet Apple-ish look; Recharts would add ~500KB to a bundle
 * that ships to every user (admin or not) and arrives with its own opinions
 * about type, grid lines and tooltips that would need overriding back to
 * these same tokens anyway. Four small charts is less code than the overrides.
 *
 * Every color is a CSS custom property, so the charts follow the theme the
 * same way the rest of the app does — no JS involved in the light/dark swap.
 *
 * Scaling: each chart declares a viewBox and gets `width: 100%; height: auto`
 * from .ad-chart, so it scales uniformly and stays crisp at any card width.
 * Hover detail is a per-column <title>, which the browser renders as a native
 * tooltip — no state, no positioning math, and it works on keyboard focus.
 */

const W = 320;
const H = 96;

/** A chart with no data yet shouldn't draw a misleading flat line at zero. */
function EmptyPlot({ label }: { label: string }) {
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label}>
      <line x1="0" y1={H - 1} x2={W} y2={H - 1} stroke="var(--line)" strokeWidth="1" />
      <text
        x={W / 2}
        y={H / 2}
        textAnchor="middle"
        dominantBaseline="middle"
        fill="var(--muted)"
        fontSize="11"
        fontFamily="inherit"
      >
        nothing recorded yet
      </text>
    </svg>
  );
}

function ChartCard({
  title,
  note,
  children,
  legend,
}: {
  title: string;
  note?: string;
  children: React.ReactNode;
  legend?: { color: string; label: string }[];
}) {
  return (
    <div className="ad-card ad-chart">
      <div className="ad-chart-head">
        <span className="ad-chart-title">{title}</span>
        {note && <span className="ad-chart-note">{note}</span>}
      </div>
      {children}
      {legend && (
        <div className="ad-legend">
          {legend.map((l) => (
            <span key={l.label}>
              <i style={{ background: l.color }} />
              {l.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** Day-by-day area chart. `series` is the filled line, `overlay` an optional
 * second line drawn on the same scale (turns against messages). */
export function ActivityChart({
  days,
  values,
  overlay,
  title,
  note,
  legend,
}: {
  days: string[];
  values: number[];
  overlay?: number[];
  title: string;
  note?: string;
  legend?: { color: string; label: string }[];
}) {
  const max = Math.max(1, ...values, ...(overlay ?? []));
  const n = values.length;
  const x = (i: number) => (n <= 1 ? 0 : (i / (n - 1)) * W);
  const y = (v: number) => H - 4 - (v / max) * (H - 12);

  if (!n || values.every((v) => v === 0) && (!overlay || overlay.every((v) => v === 0))) {
    return (
      <ChartCard title={title} note={note}>
        <EmptyPlot label={title} />
      </ChartCard>
    );
  }

  const line = values.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const area = `${line} L${W},${H - 4} L0,${H - 4} Z`;
  const overlayLine = overlay
    ?.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`)
    .join(' ');

  return (
    <ChartCard title={title} note={note} legend={legend}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
        {/* Baseline, plus a faint midline so the eye has a scale to read against. */}
        <line x1="0" y1={y(max / 2)} x2={W} y2={y(max / 2)} stroke="var(--line)" strokeWidth="1" strokeDasharray="2 4" />
        <line x1="0" y1={H - 4} x2={W} y2={H - 4} stroke="var(--line)" strokeWidth="1" />
        <path d={area} fill="var(--accent)" opacity="0.12" />
        <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.75" strokeLinejoin="round" strokeLinecap="round" />
        {overlayLine && (
          <path
            d={overlayLine}
            fill="none"
            stroke="var(--muted)"
            strokeWidth="1.4"
            strokeDasharray="3 3"
            strokeLinejoin="round"
          />
        )}
        {/* Invisible hit strips carry the per-day tooltip. */}
        {values.map((v, i) => (
          <rect key={days[i]} x={x(i) - W / n / 2} y="0" width={W / n} height={H} fill="transparent">
            <title>
              {days[i]}: {v}
              {overlay ? ` · ${overlay[i]}` : ''}
            </title>
          </rect>
        ))}
      </svg>
    </ChartCard>
  );
}

/** Errors per day as bars. Kept separate from the activity area chart because
 * errors are usually an order of magnitude smaller — on a shared axis they'd
 * flatten into the baseline and read as "no errors". */
export function ErrorChart({
  days,
  errors,
  turns,
  title,
  note,
}: {
  days: string[];
  errors: number[];
  turns: number[];
  title: string;
  note?: string;
}) {
  const max = Math.max(1, ...errors);
  const n = errors.length;
  const slot = W / Math.max(1, n);
  const barW = Math.max(2, slot * 0.62);

  if (!n || errors.every((v) => v === 0)) {
    return (
      <ChartCard title={title} note={note}>
        <EmptyPlot label={title} />
      </ChartCard>
    );
  }

  return (
    <ChartCard title={title} note={note}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
        <line x1="0" y1={H - 4} x2={W} y2={H - 4} stroke="var(--line)" strokeWidth="1" />
        {errors.map((v, i) => {
          const h = v === 0 ? 0 : Math.max(2, (v / max) * (H - 14));
          return (
            <g key={days[i]}>
              <rect
                x={i * slot + (slot - barW) / 2}
                y={H - 4 - h}
                width={barW}
                height={h}
                rx={Math.min(2, barW / 2)}
                fill="var(--danger)"
                opacity={v === 0 ? 0 : 0.85}
              />
              <rect x={i * slot} y="0" width={slot} height={H} fill="transparent">
                <title>
                  {days[i]}: {v} error{v === 1 ? '' : 's'} of {turns[i]} turn{turns[i] === 1 ? '' : 's'}
                </title>
              </rect>
            </g>
          );
        })}
      </svg>
    </ChartCard>
  );
}

/** p50 / p95 as two proportional bars. A distribution needs raw samples the
 * overview doesn't ship; two bars honestly show the two numbers it does. */
export function LatencyChart({
  p50,
  p95,
  title,
  note,
}: {
  p50: number | null;
  p95: number | null;
  title: string;
  note?: string;
}) {
  if (p50 == null && p95 == null) {
    return (
      <ChartCard title={title} note={note}>
        <EmptyPlot label={title} />
      </ChartCard>
    );
  }
  const max = Math.max(1, p50 ?? 0, p95 ?? 0);
  const rows: { label: string; value: number | null; color: string }[] = [
    { label: 'p50', value: p50, color: 'var(--accent)' },
    { label: 'p95', value: p95, color: 'var(--warn)' },
  ];
  const rowH = 30;
  const chartH = rows.length * rowH + 8;
  const labelW = 30;

  return (
    <ChartCard title={title} note={note}>
      <svg viewBox={`0 0 ${W} ${chartH}`} role="img" aria-label={title}>
        {rows.map((r, i) => {
          const y = i * rowH + 6;
          const full = W - labelW - 46;
          const w = r.value == null ? 0 : Math.max(2, (r.value / max) * full);
          return (
            <g key={r.label}>
              <text x="0" y={y + 11} fill="var(--muted)" fontSize="11" fontFamily="inherit">
                {r.label}
              </text>
              <rect x={labelW} y={y} width={full} height="16" rx="8" fill="var(--fill)" />
              <rect x={labelW} y={y} width={w} height="16" rx="8" fill={r.color} />
              <text
                x={labelW + full + 6}
                y={y + 11}
                fill="var(--ink)"
                fontSize="11"
                fontFamily="inherit"
                fontWeight="600"
              >
                {r.value == null ? '—' : r.value < 1000 ? `${r.value}ms` : `${(r.value / 1000).toFixed(1)}s`}
              </text>
            </g>
          );
        })}
      </svg>
    </ChartCard>
  );
}

/** Thumbs up against thumbs down, as one split bar. The ratio is the point,
 * so a single bar reads faster than two. */
export function ThumbsChart({
  up,
  down,
  title,
  note,
}: {
  up: number;
  down: number;
  title: string;
  note?: string;
}) {
  const total = up + down;
  if (total === 0) {
    return (
      <ChartCard title={title} note={note}>
        <EmptyPlot label={title} />
      </ChartCard>
    );
  }
  const barH = 22;
  const chartH = barH + 34;
  const upW = (up / total) * W;

  return (
    <ChartCard
      title={title}
      note={note}
      legend={[
        { color: 'var(--success)', label: `${up} up` },
        { color: 'var(--danger)', label: `${down} down` },
      ]}
    >
      <svg viewBox={`0 0 ${W} ${chartH}`} role="img" aria-label={title}>
        {/* Rounded ends on the outside only, so the split reads as one bar. */}
        <clipPath id="ad-thumbs-clip">
          <rect x="0" y="0" width={W} height={barH} rx={barH / 2} />
        </clipPath>
        <g clipPath="url(#ad-thumbs-clip)">
          <rect x="0" y="0" width={upW} height={barH} fill="var(--success)">
            <title>{up} thumbs up</title>
          </rect>
          <rect x={upW} y="0" width={W - upW} height={barH} fill="var(--danger)">
            <title>{down} thumbs down</title>
          </rect>
        </g>
        <text x="0" y={barH + 22} fill="var(--ink)" fontSize="13" fontWeight="650" fontFamily="inherit">
          {Math.round((up / total) * 100)}% positive
        </text>
        <text
          x={W}
          y={barH + 22}
          textAnchor="end"
          fill="var(--muted)"
          fontSize="11"
          fontFamily="inherit"
        >
          {total} rated turn{total === 1 ? '' : 's'}
        </text>
      </svg>
    </ChartCard>
  );
}
