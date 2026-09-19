import { useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { ApiError } from '../api';
import Logo from '../components/Logo';

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
      // login() resolves with the freshly-read status; the context state
      // this render closed over is still the pre-login one.
      const { hasClients, llmConfigured } = await login(email, password);
      navigate(!hasClients ? '/onboarding' : llmConfigured ? '/app/chat' : '/setup', { replace: true });
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? 'Invalid email or password.'
          : err instanceof ApiError && err.status === 429
            ? 'Too many attempts — wait a minute and try again.'
            : 'Something went wrong. Try again.',
      );
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
        <Logo size={64} className="login-mark" />
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

        <p style={{ fontSize: 12.5, color: 'var(--muted)', textAlign: 'center', margin: 0, lineHeight: 1.6 }}>
          <Link to="/reset" style={{ color: 'var(--accent)', fontWeight: 600, textDecoration: 'none' }}>Forgot your password?</Link>
          <br />
          New here? <Link to="/signup" style={{ color: 'var(--accent)', fontWeight: 600, textDecoration: 'none' }}>Create an account</Link>
        </p>
      </form>
    </div>
  );
}
