import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';

const tabStyle = (active: boolean): React.CSSProperties => ({
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  height: 34,
  padding: '0 14px',
  borderRadius: 8,
  fontSize: 13.5,
  fontWeight: 600,
  textDecoration: 'none',
  color: active ? '#ffffff' : 'var(--ink)',
  background: active ? 'var(--accent)' : 'transparent',
});

export default function AppShell() {
  const { logout } = useAuth();
  const navigate = useNavigate();

  async function onLogout() {
    await logout();
    navigate('/login', { replace: true });
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', background: 'var(--card)' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 14,
          padding: '10px 20px',
          borderBottom: '1px solid var(--line)',
          flexShrink: 0,
        }}
      >
        <div
          style={{
            width: 30,
            height: 30,
            borderRadius: 9,
            background: 'linear-gradient(155deg, var(--accent), var(--success))',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M18 8a6 6 0 0 0-12 0c0 3 1.5 5 2 7h8c.5-2 2-4 2-7Z" />
            <path d="M9 21h6M10 18h4" />
          </svg>
        </div>
        <span style={{ fontSize: 14.5, fontWeight: 700 }}>Aboyeur</span>

        <nav style={{ display: 'flex', gap: 4, marginLeft: 12, background: 'var(--fill)', borderRadius: 10, padding: 3 }}>
          <NavLink to="/app/chat" style={({ isActive }) => tabStyle(isActive)}>
            Chat
          </NavLink>
          <NavLink to="/app/library" style={({ isActive }) => tabStyle(isActive)}>
            Library
          </NavLink>
        </nav>

        <div style={{ flex: 1 }} />

        <NavLink
          to="/onboarding"
          style={{
            width: 32,
            height: 32,
            borderRadius: '50%',
            background: 'var(--fill)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'var(--ink)',
          }}
          title="Edit profile"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M20 21a8 8 0 0 0-16 0M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z" />
          </svg>
        </NavLink>
        <button
          type="button"
          onClick={onLogout}
          className="btn-ghost"
          style={{ height: 32, padding: '0 14px', fontSize: 13 }}
        >
          Sign out
        </button>
      </div>

      <div style={{ flex: 1, minHeight: 0 }}>
        <Outlet />
      </div>
    </div>
  );
}
