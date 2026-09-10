import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import * as api from '../api';

interface AuthState {
  userId: number | null;
  loading: boolean;
  /** Operator access to /admin. Server-checked on every admin call — this
   * only decides whether the nav entry and route tree render at all. */
  isAdmin: boolean;
  /** Whether this user has saved an LLM endpoint + key (the /setup gate). */
  llmConfigured: boolean;
  refreshLlmStatus: () => Promise<boolean>;
  /** Whether this practitioner has any clients — false only before onboarding. */
  hasClients: boolean;
  refreshClientStatus: () => Promise<boolean>;
  /** Resolves with the freshly-read status, so the caller can route on it
   * without waiting a render for the context state to catch up. */
  login: (email: string, password: string) => Promise<{ hasClients: boolean; llmConfigured: boolean }>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [userId, setUserId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [isAdmin, setIsAdmin] = useState(false);
  const [llmConfigured, setLlmConfigured] = useState(false);
  const [hasClients, setHasClients] = useState(false);

  const refreshLlmStatus = useCallback(async () => {
    let configured = false;
    try {
      configured = (await api.getLlmSettings()).configured;
    } catch {
      // 401 (logged out) or a backend hiccup — treat as unconfigured rather
      // than blocking the app on a status call.
    }
    setLlmConfigured(configured);
    return configured;
  }, []);

  const refreshClientStatus = useCallback(async () => {
    let present = false;
    try {
      present = (await api.listClients()).length > 0;
    } catch {
      /* no clients yet, or the call failed — either way, onboarding first */
    }
    setHasClients(present);
    return present;
  }, []);

  useEffect(() => {
    // Both calls before `loading` flips: routing keys off llmConfigured, and
    // resolving them separately would flash the /setup screen at a user who
    // is in fact configured.
    api
      .me()
      .then(async (res) => {
        setUserId(res.user_id);
        setIsAdmin(res.is_admin);
        await Promise.all([refreshLlmStatus(), refreshClientStatus()]);
      })
      .catch(() => setUserId(null))
      .finally(() => setLoading(false));
  }, [refreshLlmStatus, refreshClientStatus]);

  const login = useCallback(
    async (email: string, password: string) => {
      const res = await api.login(email, password);
      setUserId(res.user_id);
      // The login response doesn't carry the flag; /auth/me does. One extra
      // round trip on sign-in only, so the admin nav is right immediately
      // instead of appearing after a reload.
      const [llm, clientsPresent] = await Promise.all([refreshLlmStatus(), refreshClientStatus()]);
      try {
        setIsAdmin((await api.me()).is_admin);
      } catch {
        setIsAdmin(false);
      }
      return { hasClients: clientsPresent, llmConfigured: llm };
    },
    [refreshLlmStatus, refreshClientStatus],
  );

  const logout = useCallback(async () => {
    await api.logout();
    setUserId(null);
    setIsAdmin(false);
    setLlmConfigured(false);
    setHasClients(false);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        userId,
        loading,
        isAdmin,
        llmConfigured,
        refreshLlmStatus,
        hasClients,
        refreshClientStatus,
        login,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}
