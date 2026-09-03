import { Navigate, Route, Routes } from 'react-router-dom';
import type { ReactNode } from 'react';
import { useAuth } from './auth/AuthContext';
import Login from './routes/Login';
import Onboarding from './routes/Onboarding';
import AppShell from './routes/AppShell';
import Library from './routes/Library';
import Chat from './routes/Chat';

function RequireAuth({ children }: { children: ReactNode }) {
  const { userId, loading } = useAuth();
  if (loading) return null;
  if (!userId) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  const { userId, loading } = useAuth();

  if (loading) return null;

  return (
    <Routes>
      <Route path="/login" element={userId ? <Navigate to="/app/chat" replace /> : <Login />} />
      <Route
        path="/onboarding"
        element={
          <RequireAuth>
            <Onboarding />
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
        <Route index element={<Navigate to="chat" replace />} />
        <Route path="chat" element={<Chat />} />
        <Route path="library" element={<Library />} />
      </Route>
      <Route path="*" element={<Navigate to={userId ? '/app/chat' : '/login'} replace />} />
    </Routes>
  );
}
