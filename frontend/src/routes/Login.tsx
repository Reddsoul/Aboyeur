import { useState } from 'react';
import type { FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { ApiError } from '../api';

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email, password);
      navigate('/app/chat', { replace: true });
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? 'Invalid email or password.' : 'Something went wrong. Try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      style={{
        position: 'relative',
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'var(--bg)',
        overflow: 'hidden',
        padding: 24,
      }}
    >
      <div
        style={{
          position: 'absolute',
          top: -160,
          left: -100,
          width: 480,
          height: 480,
          borderRadius: '50%',
          background: 'linear-gradient(155deg, var(--accent), var(--success))',
          opacity: 0.14,
          filter: 'blur(90px)',
        }}
      />
      <form
        onSubmit={onSubmit}
        style={{
          position: 'relative',
          width: '100%',
          maxWidth: 380,
          background: 'var(--card)',
          borderRadius: 24,
          boxShadow: '0 30px 70px rgba(0,0,0,0.16), 0 2px 8px rgba(0,0,0,0.06)',
          padding: '40px 36px',
          display: 'flex',
          flexDirection: 'column',
          gap: 18,
        }}
      >
        <div
          style={{
            width: 60,
            height: 60,
            borderRadius: 16,
            background: 'linear-gradient(155deg, var(--accent), var(--success))',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            margin: '0 auto 4px',
          }}
        >
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M18 8a6 6 0 0 0-12 0c0 3 1.5 5 2 7h8c.5-2 2-4 2-7Z" />
            <path d="M9 21h6M10 18h4" />
          </svg>
        </div>
        <div style={{ textAlign: 'center' }}>
          <h1 style={{ fontSize: 24, fontWeight: 700, margin: '0 0 4px', letterSpacing: '-0.01em' }}>Sign in to Aboyeur</h1>
          <p style={{ fontSize: 14, color: 'var(--muted)', margin: 0 }}>Your personal nutritionist and menu planner.</p>
        </div>

        <div>
          <label style={{ display: 'block', fontSize: 13, color: 'var(--muted)', marginBottom: 6, fontWeight: 500 }}>Email</label>
          <input
            className="text-input"
            type="email"
            required
            autoFocus
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
          />
        </div>
        <div>
          <label style={{ display: 'block', fontSize: 13, color: 'var(--muted)', marginBottom: 6, fontWeight: 500 }}>Password</label>
          <input
            className="text-input"
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
          />
        </div>

        {error && <div style={{ color: 'var(--danger)', fontSize: 13.5 }}>{error}</div>}

        <button type="submit" className="btn-primary" disabled={busy} style={{ marginTop: 6 }}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>

        <p style={{ fontSize: 12.5, color: 'var(--muted)', textAlign: 'center', margin: 0 }}>
          Invite-only — accounts are created by the Aboyeur team.
        </p>
      </form>
    </div>
  );
}
