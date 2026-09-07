import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import * as api from '../api';
import { useAuth } from '../auth/AuthContext';

/** The practitioner's book, plus which client the app is currently acting for.
 *
 * The active client is the scope for chat, the profile editor and plans, so it
 * is deliberately explicit state rather than something inferred per screen —
 * every place that can write client data reads it from here, and the header
 * always shows it. Persisted per browser so a reload doesn't silently drop you
 * onto a different person. */
interface ClientState {
  clients: api.Client[];
  loading: boolean;
  activeClient: api.Client | null;
  setActiveClient: (client: api.Client | number | null) => void;
  refreshClients: () => Promise<api.Client[]>;
}

const ClientContext = createContext<ClientState | null>(null);

const ACTIVE_KEY = 'aboyeur.activeClientId';

function readStoredId(): number | null {
  try {
    const raw = localStorage.getItem(ACTIVE_KEY);
    return raw ? Number(raw) || null : null;
  } catch {
    return null; // private window / blocked storage — just start unset
  }
}

export function ClientProvider({ children }: { children: ReactNode }) {
  const { userId } = useAuth();
  const [clients, setClients] = useState<api.Client[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeId, setActiveId] = useState<number | null>(readStoredId);

  const refreshClients = useCallback(async () => {
    if (!userId) {
      setClients([]);
      setLoading(false);
      return [];
    }
    try {
      const list = await api.listClients();
      setClients(list);
      setActiveId((current) => {
        // Don't strand the app on a client that was archived or deleted
        // elsewhere; fall back to the first one on the book.
        if (current && list.some((c) => c.id === current)) return current;
        return list.length ? list[0].id : null;
      });
      return list;
    } catch {
      setClients([]);
      return [];
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    void refreshClients();
  }, [refreshClients]);

  useEffect(() => {
    try {
      if (activeId == null) localStorage.removeItem(ACTIVE_KEY);
      else localStorage.setItem(ACTIVE_KEY, String(activeId));
    } catch {
      /* storage blocked — the id still lives in memory for this session */
    }
  }, [activeId]);

  const setActiveClient = useCallback((client: api.Client | number | null) => {
    setActiveId(client == null ? null : typeof client === 'number' ? client : client.id);
  }, []);

  const activeClient = useMemo(
    () => clients.find((c) => c.id === activeId) ?? null,
    [clients, activeId],
  );

  return (
    <ClientContext.Provider
      value={{ clients, loading, activeClient, setActiveClient, refreshClients }}
    >
      {children}
    </ClientContext.Provider>
  );
}

export function useClients(): ClientState {
  const ctx = useContext(ClientContext);
  if (!ctx) throw new Error('useClients must be used inside ClientProvider');
  return ctx;
}
