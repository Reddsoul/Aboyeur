import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { useClients } from '../clients/ClientContext';
import Logo from '../components/Logo';
import TosModal from '../components/TosModal';
import VerifyBanner from '../components/VerifyBanner';

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
  const { logout, isAdmin } = useAuth();
  const { clients, activeClient, setActiveClient } = useClients();
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
        <Logo size={30} />
        <span style={{ fontSize: 14.5, fontWeight: 700 }}>Aboyeur</span>

        <nav style={{ display: 'flex', gap: 4, marginLeft: 12, background: 'var(--fill)', borderRadius: 10, padding: 3 }}>
          <NavLink to="/app/chat" style={({ isActive }) => tabStyle(isActive)}>
            Chat
          </NavLink>
          <NavLink to="/app/library" style={({ isActive }) => tabStyle(isActive)}>
            Library
          </NavLink>
          <NavLink to="/app/clients" style={({ isActive }) => tabStyle(isActive)}>
            Clients
          </NavLink>
        </nav>

        {/* Which client the app is acting for. Always on screen: chat, the
            profile editor and every plan are scoped to whoever is picked here,
            so it must never be a guess. */}
        <label
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginLeft: 6,
            padding: '0 10px 0 12px',
            height: 34,
            borderRadius: 10,
            background: 'var(--fill)',
            fontSize: 13,
            color: 'var(--muted)',
          }}
          title="Everything you do applies to this client"
        >
          Planning for
          <select
            value={activeClient?.id ?? ''}
            onChange={(e) => setActiveClient(e.target.value ? Number(e.target.value) : null)}
            style={{
              border: 'none',
              background: 'transparent',
              color: 'var(--ink)',
              fontSize: 13.5,
              fontWeight: 600,
              maxWidth: 160,
            }}
          >
            {clients.length === 0 && <option value="">no clients yet</option>}
            {clients.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>

        <div style={{ flex: 1 }} />

        {/* Only the operator sees this. The console is a different place, so
            it sits apart from the Chat/Library/Clients tab group rather than
            inside it. */}
        {isAdmin && (
          <NavLink
            to="/admin"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              height: 32,
              padding: '0 12px',
              borderRadius: 999,
              background: 'color-mix(in srgb, var(--accent) 15%, transparent)',
              color: 'var(--accent)',
              fontSize: 12.5,
              fontWeight: 700,
              textDecoration: 'none',
            }}
            title="Operator console"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 3v18h18M7 15l4-5 3 3 5-7" />
            </svg>
            Admin
          </NavLink>
        )}

        <NavLink
          to="/app/settings"
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
          title="Settings"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" />
          </svg>
        </NavLink>
        <NavLink
          to="/app/account"
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
          title="Your account"
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

      <VerifyBanner />
      <div style={{ flex: 1, minHeight: 0 }}>
        <Outlet />
      </div>
      {/* Blocks everything until the current terms are accepted. */}
      <TosModal />
    </div>
  );
}
