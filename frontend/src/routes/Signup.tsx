import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import './Auth.css';
import * as api from '../api';
import { ApiError } from '../api';
import { useAuth } from '../auth/AuthContext';
import Logo from '../components/Logo';
import { ClinicalNote } from '../components/Legal';
import { COUNTRIES } from '../countries';

export default function Signup() {
  const navigate = useNavigate();
  const { refreshMe, refreshClientStatus } = useAuth();
  const [params] = useSearchParams();
  const [mode, setMode] = useState<{ mode: 'invite' | 'open'; legal_version: string } | null>(null);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [country, setCountry] = useState('');
  const [invite, setInvite] = useState(params.get('invite') ?? '');
  const [agree, setAgree] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.signupMode().then(setMode).catch(() => setMode({ mode: 'invite', legal_version: '' }));
  }, []);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!mode || !agree) return;
    setError(null);
    setBusy(true);
    try {
      await api.signup({
        email,
        password,
        country: country || null,
        invite_code: invite || null,
        tos_version: mode.legal_version,
      });
      await Promise.all([refreshMe(), refreshClientStatus()]);
      navigate('/onboarding', { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        const byCode: Record<string, string> = {
          invite_required: 'An invite code is required during the private beta.',
          invite_invalid: "That invite code isn't valid.",
          invite_used: 'That invite code has already been used.',
          invite_expired: 'That invite code has expired.',
          taken: 'An account already exists for that email — sign in instead.',
          pwned: 'That password appears in a known data breach. Choose a different one.',
          rate_limited: 'Too many attempts from this network. Try again in an hour.',
        };
        setError((err.code && byCode[err.code]) || err.message);
      } else {
        setError("Couldn't reach Aboyeur — check your connection.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="au-stage">
      <div className="au-orb" />
      <form className="au-card" onSubmit={onSubmit}>
        <Logo size={56} className="login-mark" />
        <div style={{ textAlign: 'center' }}>
          <h1 className="au-title">Create your account</h1>
          <p className="au-sub">Free with your own API key. Pro unlocks hosted models.</p>
        </div>

        <div>
          <label className="au-label">Email</label>
          <input className="text-input" type="email" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
        </div>
        <div>
          <label className="au-label">Password</label>
          <input className="text-input" type="password" required minLength={10} autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="At least 10 characters" />
        </div>
        <div>
          <label className="au-label">Country (for pricing — optional)</label>
          <select className="text-input" value={country} onChange={(e) => setCountry(e.target.value)}>
            <option value="">Detect automatically</option>
            {COUNTRIES.map(([code, name]) => (
              <option key={code} value={code}>{name}</option>
            ))}
          </select>
        </div>
        {mode?.mode === 'invite' && (
          <div>
            <label className="au-label">Invite code</label>
            <input className="text-input" required value={invite} onChange={(e) => setInvite(e.target.value)} placeholder="From your invitation" />
          </div>
        )}

        <label className="au-check">
          <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
          <span>
            I'm 18 or older and I agree to the <Link to="/legal/terms" target="_blank" rel="noreferrer" style={{ color: 'var(--accent)' }}>Terms of Service</Link>,{' '}
            <Link to="/legal/privacy" target="_blank" rel="noreferrer" style={{ color: 'var(--accent)' }}>Privacy Policy</Link> and{' '}
            <Link to="/legal/disclaimer" target="_blank" rel="noreferrer" style={{ color: 'var(--accent)' }}>Health Disclaimer</Link>.
          </span>
        </label>

        <ClinicalNote />

        {error && <div className="au-error">{error}</div>}

        <button type="submit" className="btn-primary" disabled={busy || !agree || !mode}>
          {busy ? 'Creating…' : 'Create account'}
        </button>
        <p className="au-fine">
          Already have one? <Link to="/login">Sign in</Link>
        </p>
      </form>
    </div>
  );
}
