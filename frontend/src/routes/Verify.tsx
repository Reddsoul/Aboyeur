import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import './Auth.css';
import * as api from '../api';
import { useAuth } from '../auth/AuthContext';
import Logo from '../components/Logo';

/** Landing page for the emailed verification link: /verify?token=… */
export default function Verify() {
  const [params] = useSearchParams();
  const { userId, refreshMe } = useAuth();
  const token = params.get('token') ?? '';
  const [state, setState] = useState<'working' | 'ok' | 'bad'>('working');
  const [detail, setDetail] = useState('');

  useEffect(() => {
    if (!token) {
      setState('bad');
      setDetail('This link is missing its token.');
      return;
    }
    api
      .verifyEmail(token)
      .then(async () => {
        setState('ok');
        await refreshMe();
      })
      .catch((err) => {
        setState('bad');
        setDetail(err instanceof api.ApiError ? err.message : "Couldn't reach Aboyeur.");
      });
  }, [token, refreshMe]);

  return (
    <div className="au-stage">
      <div className="au-orb" />
      <div className="au-card">
        <Logo size={56} className="login-mark" />
        <div style={{ textAlign: 'center' }}>
          {state === 'working' && <h1 className="au-title">Verifying…</h1>}
          {state === 'ok' && (
            <>
              <h1 className="au-title">Email verified</h1>
              <p className="au-sub">You're all set. Chat and billing are unlocked.</p>
            </>
          )}
          {state === 'bad' && (
            <>
              <h1 className="au-title">That link didn't work</h1>
              <p className="au-sub">{detail}</p>
            </>
          )}
        </div>
        {state !== 'working' && (
          <Link to={userId ? '/app' : '/login'} className="btn-primary" style={{ textAlign: 'center', textDecoration: 'none' }}>
            {userId ? 'Continue' : 'Sign in'}
          </Link>
        )}
        {state === 'bad' && userId && (
          <p className="au-fine">Request a new link from the banner at the top of the app.</p>
        )}
      </div>
    </div>
  );
}
