import { Navigate, Route, Routes, useSearchParams } from 'react-router-dom';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { useAuth } from './auth/AuthContext';
import Login from './routes/Login';
import Landing from './routes/Landing';
import Signup from './routes/Signup';
import Verify from './routes/Verify';
import Reset from './routes/Reset';
import Legal from './routes/Legal';
import Onboarding from './routes/Onboarding';
import AppShell from './routes/AppShell';
import Library from './routes/Library';
import Chat from './routes/Chat';
import Settings from './routes/Settings';
import Account from './routes/Account';
import Clients from './routes/Clients';
import ClientDetail from './routes/ClientDetail';
import AdminShell from './routes/admin/AdminShell';
import Dashboard from './routes/admin/Dashboard';
import AdminUsers from './routes/admin/Users';
import AdminUserDetail from './routes/admin/UserDetail';
import AdminActivity from './routes/admin/Activity';
import AdminFeedback from './routes/admin/Feedback';
import AdminTranscript from './routes/admin/Transcript';
import AdminBilling from './routes/admin/Billing';
import AdminProviders from './routes/admin/Providers';
import AdminInvites from './routes/admin/Invites';

function OnboardingGate() {
  const { hasClients } = useAuth();
  const [params] = useSearchParams();
  // Decided once, on mount. Finishing the wizard flips hasProfile true, and
  // re-deciding then would redirect the user out of the tour they just earned.
  const [redirect] = useState(() => hasClients && params.get('tour') !== '1');
  if (redirect) return <Navigate to="/app/clients" replace />;
  return <Onboarding />;
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { userId, loading } = useAuth();
  if (loading) return null;
  if (!userId) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** Guards the whole /admin tree. A non-admin never renders the shell at all —
 * they land back in the app. This is convenience, not security: the console's
 * code ships in everyone's bundle, and every /admin API call is checked
 * server-side by require_admin regardless of what the client decides here. */
function RequireAdmin({ children }: { children: ReactNode }) {
  const { userId, isAdmin, loading } = useAuth();
  if (loading) return null;
  if (!userId) return <Navigate to="/login" replace />;
  if (!isAdmin) return <Navigate to="/app" replace />;
  return <>{children}</>;
}

export default function App() {
  const { userId, loading, llmConfigured, hasClients } = useAuth();

  if (loading) return null;

  // Where a signed-in user goes when they haven't asked for anywhere in
  // particular: a first client (the wizard, once ever), then the LLM, then
  // chat. Redirect targets only, never walls — every screen stays reachable
  // from the nav, and Chat/Settings carry their own nudges.
  const home = !hasClients ? '/onboarding' : llmConfigured ? '/app/chat' : '/setup';

  return (
    <Routes>
      {/* Logged-out front door. A signed-in visitor goes straight in. */}
      <Route path="/" element={userId ? <Navigate to={home} replace /> : <Landing />} />
      <Route path="/login" element={userId ? <Navigate to={home} replace /> : <Login />} />
      <Route path="/signup" element={userId ? <Navigate to={home} replace /> : <Signup />} />
      <Route path="/verify" element={<Verify />} />
      <Route path="/reset" element={<Reset />} />
      {/* Public: the signup form and the ToS modal both link here. */}
      <Route path="/legal" element={<Navigate to="/legal/terms" replace />} />
      <Route path="/legal/:slug" element={<Legal />} />
      {/* The welcome wizard is a first-login screen. Once a household exists
          it hands off to the editor, unless ?tour=1 asks to replay the tour. */}
      <Route
        path="/onboarding"
        element={
          <RequireAuth>
            <OnboardingGate />
          </RequireAuth>
        }
      />
      <Route
        path="/setup"
        element={
          <RequireAuth>
            <Settings firstRun />
          </RequireAuth>
        }
      />
      <Route
        path="/app"
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to={home} replace />} />
        <Route path="chat" element={<Chat />} />
        <Route path="library" element={<Library />} />
        <Route path="clients" element={<Clients />} />
        <Route path="clients/:clientId" element={<ClientDetail />} />
        <Route path="account" element={<Account />} />
        {/* Pre-clients deep link: a "profile" used to mean the household. */}
        <Route path="profile" element={<Navigate to="/app/account" replace />} />
        <Route path="settings" element={<Settings />} />
      </Route>
      {/* The operator console — its own shell, its own nav, same app. */}
      <Route
        path="/admin"
        element={
          <RequireAdmin>
            <AdminShell />
          </RequireAdmin>
        }
      >
        <Route index element={<Dashboard />} />
        <Route path="users" element={<AdminUsers />} />
        <Route path="users/:userId" element={<AdminUserDetail />} />
        <Route path="activity" element={<AdminActivity />} />
        <Route path="feedback" element={<AdminFeedback />} />
        <Route path="sessions/:sessionId" element={<AdminTranscript />} />
        <Route path="billing" element={<AdminBilling />} />
        <Route path="providers" element={<AdminProviders />} />
        <Route path="invites" element={<AdminInvites />} />
      </Route>
      <Route path="*" element={<Navigate to={userId ? home : '/'} replace />} />
    </Routes>
  );
}
