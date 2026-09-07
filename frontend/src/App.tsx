import { Navigate, Route, Routes, useSearchParams } from 'react-router-dom';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { useAuth } from './auth/AuthContext';
import Login from './routes/Login';
import Onboarding from './routes/Onboarding';
import AppShell from './routes/AppShell';
import Library from './routes/Library';
import Chat from './routes/Chat';
import Settings from './routes/Settings';
import Clients from './routes/Clients';
import ClientDetail from './routes/ClientDetail';

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
      <Route path="/login" element={userId ? <Navigate to={home} replace /> : <Login />} />
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
        {/* Pre-clients deep link: the profile now lives under its client. */}
        <Route path="profile" element={<Navigate to="/app/clients" replace />} />
        <Route path="settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<Navigate to={userId ? home : '/login'} replace />} />
    </Routes>
  );
}
