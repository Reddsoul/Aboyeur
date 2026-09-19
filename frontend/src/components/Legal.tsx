import { Link } from 'react-router-dom';

/** One line, everywhere numbers or plans are shown. Required by the
 * disclaimer policy — see legal/disclaimer.md. */
export function DisclaimerLink({ style }: { style?: React.CSSProperties }) {
  return (
    <span style={{ fontSize: 12, color: 'var(--muted)', ...style }}>
      Estimates, not medical advice.{' '}
      <Link to="/legal/disclaimer" style={{ color: 'var(--accent)', fontWeight: 600, textDecoration: 'none' }}>
        Nutrition &amp; health disclaimer
      </Link>
    </span>
  );
}

export function ClinicalNote() {
  return (
    <p className="au-note">
      <strong>Not for clinical use.</strong> Aboyeur is for coaches, trainers, nutrition consultants and
      self-tracking. It is not HIPAA-compliant, offers no BAA, and must not be used to handle patient
      data for a healthcare provider.
    </p>
  );
}

export function LegalFooter({ style }: { style?: React.CSSProperties }) {
  const link = { color: 'var(--muted)', textDecoration: 'none' } as const;
  return (
    <div style={{ display: 'flex', gap: 12, fontSize: 12, color: 'var(--muted)', flexWrap: 'wrap', ...style }}>
      <Link to="/legal/terms" style={link}>Terms</Link>
      <Link to="/legal/privacy" style={link}>Privacy</Link>
      <Link to="/legal/disclaimer" style={link}>Disclaimer</Link>
      <Link to="/legal/refunds" style={link}>Refunds</Link>
      <Link to="/legal/acceptable-use" style={link}>Acceptable use</Link>
    </div>
  );
}
