import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import './styles/tokens.css';
import App from './App';
import { AuthProvider } from './auth/AuthContext';
import { ClientProvider } from './clients/ClientContext';

// Error reporting, only when a DSN is baked in at build time. No PII: no
// session replay, no user context, request bodies scrubbed by the backend's
// matching before_send. Chat text never reaches Sentry from here — the
// only things captured are uncaught exceptions and their stacks.
const dsn = import.meta.env.VITE_SENTRY_DSN as string | undefined;
if (dsn) {
  void import('@sentry/react').then((Sentry) => {
    Sentry.init({
      dsn,
      sendDefaultPii: false,
      tracesSampleRate: 0,
      beforeSend(event) {
        if (event.request) delete event.request.cookies;
        if (event.user) delete event.user.email;
        return event;
      },
    });
  });
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <ClientProvider>
          <App />
        </ClientProvider>
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
);
