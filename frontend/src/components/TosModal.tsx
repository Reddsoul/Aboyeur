import { useState } from 'react';
import { Link } from 'react-router-dom';
import '../routes/Auth.css';
import * as api from '../api';
import { useAuth } from '../auth/AuthContext';

/** Blocks the app when the server's LEGAL_VERSION is newer than what this
 * account accepted. Renders nothing otherwise. Mounted once in AppShell. */
export default function TosModal() {
  const { me, refreshMe } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!me || !me.tos.needs_acceptance) return null;

  async function accept() {
    setBusy(true);
    setError(null);
    try {
      await api.acceptTos(me!.tos.current);
      await refreshMe();
    } catch {
      setError("Couldn't record your acceptance — try again.");
    } finally {
      setBusy(false);
    }
  }

  const first = !me.tos.accepted;
  return (
    <div className="au-modal-backdrop" role="dialog" aria-modal="true">
      <div className="au-modal">
        <h2 style={{ margin: 0, fontSize: 20 }}>{first ? 'Before you continue' : 'Updated terms'}</h2>
        <p className="au-sub">
          {first
            ? 'Please review and accept the Terms of Service and Privacy Policy to keep using Aboyeur.'
            : `Our Terms of Service and Privacy Policy changed (version ${me.tos.current}). Please review and accept the new version to continue.`}
        </p>
        <div className="au-links" style={{ justifyContent: 'flex-start' }}>
          <Link to="/legal/terms" target="_blank" rel="noreferrer">Terms of Service</Link>
          <Link to="/legal/privacy" target="_blank" rel="noreferrer">Privacy Policy</Link>
          <Link to="/legal/disclaimer" target="_blank" rel="noreferrer">Health disclaimer</Link>
        </div>
        <p className="au-note">
          <strong>Not for clinical use.</strong> Aboyeur is not HIPAA-compliant and must not be used to
          process patient data for a healthcare provider.
        </p>
        {error && <div className="au-error">{error}</div>}
        <button type="button" className="btn-primary" disabled={busy} onClick={() => void accept()}>
          {busy ? 'Saving…' : 'I accept'}
        </button>
      </div>
    </div>
  );
}
