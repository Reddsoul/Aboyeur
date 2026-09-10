/** Formatting shared across the admin screens. Every timestamp the backend
 * sends is ISO8601 UTC with a trailing Z, so `new Date()` parses it correctly
 * and renders in the operator's own timezone — which is what they want when
 * they're asking "what happened at 3am". */

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return 'never';
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return 'never';
  if (ms < 60_000) return 'just now';
  const mins = Math.floor(ms / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(months / 12)}y ago`;
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function clockTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/** Latency. Sub-second stays in ms; past that seconds read faster. */
export function duration(ms: number | null | undefined): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
}

export function count(n: number | null | undefined): string {
  if (n == null) return '—';
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** A rate the backend leaves null until there's anything to divide by — "—"
 * is honest there, "0%" would be a claim we can't make. */
export function percent(rate: number | null | undefined): string {
  if (rate == null) return '—';
  return `${(rate * 100).toFixed(rate < 0.1 ? 1 : 0)}%`;
}

/** Flatten a markdown excerpt into a readable one-liner.
 *
 * The feedback board quotes the rated answer as plain text, and the agent's
 * answers are markdown — so a table came through as a wall of pipes and
 * dashes. This is a display-only squash, never applied to anything stored:
 * the full message is one click away on the transcript, rendered properly. */
export function plainExcerpt(md: string | null | undefined): string {
  if (!md) return '';
  return md
    .replace(/```[\s\S]*?```/g, ' ')       // fenced code
    .replace(/^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/gm, ' ') // table rules
    .replace(/\|/g, ' · ')                 // remaining table cells
    .replace(/[*_`#>]/g, '')               // emphasis, headings, quotes
    .replace(/\s*·\s*(?=·)/g, '')          // collapse empty cells
    .replace(/\s+/g, ' ')
    .trim();
}

/** How a feedback status is spelled on screen. Shared by the board and the
 * transcript so the same row never reads "In progress" in one place and
 * "in_progress" in the other. */
export const STATUS_LABEL: Record<string, string> = {
  new: 'New',
  triaged: 'Triaged',
  in_progress: 'In progress',
  resolved: 'Resolved',
  wontfix: "Won't fix",
};

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}
