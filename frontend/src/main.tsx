import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import './styles/tokens.css';
import App from './App';
import { AuthProvider } from './auth/AuthContext';
import { ClientProvider } from './clients/ClientContext';

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
