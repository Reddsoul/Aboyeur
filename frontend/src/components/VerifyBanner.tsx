import { useState } from 'react';
import '../routes/Auth.css';
import * as api from '../api';
import { useAuth } from '../auth/AuthContext';

/** Shown until the email is verified: chat and purchases stay closed
 * (server-enforced) and this is where the resend lives. */
export default function VerifyBanner() {
  const { me } = useAuth();
  const [sent, setSent] = useState<string | null>(null);
  if (!me || me.email_verified) return null;

  async function resend() {
    try {
      await api.resendVerification();
      setSent('Sent — check your inbox (and spam).');
    } catch (err) {
      setSent(err instanceof api.ApiError && err.status === 429 ? 'Too many requests — try later.' : "Couldn't send.");
    }
  }

  return (
    <div className="au-banner">
      <span>
        <strong>Verify your email</strong> to start chatting. We sent a link when you signed up.
      </span>
      <button type="button" className="btn-ghost" onClick={() => void resend()}>
        Resend link
      </button>
      {sent && <span style={{ color: 'var(--muted)' }}>{sent}</span>}
    </div>
  );
}
