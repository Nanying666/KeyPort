'use client';

import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { api, ApiError } from './api';

interface AuthCtx {
  authenticated: boolean;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const Ctx = createContext<AuthCtx>({
  authenticated: false,
  loading: true,
  login: async () => {},
  logout: async () => {},
  refresh: async () => {},
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [authenticated, setAuthenticated] = useState(false);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const res = await api.get<{ authenticated: boolean }>('/api/session');
      setAuthenticated(res.authenticated);
    } catch {
      setAuthenticated(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    await api.post('/api/login', { username, password });
    setAuthenticated(true);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post('/api/logout');
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
    }
    setAuthenticated(false);
  }, []);

  return (
    <Ctx.Provider value={{ authenticated, loading, login, logout, refresh }}>
      {children}
    </Ctx.Provider>
  );
}

export const useAuth = () => useContext(Ctx);
