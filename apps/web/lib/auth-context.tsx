"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";

import { apiFetch } from "./api";

interface AuthUser {
  id: string;
  email: string;
  orgId: string;
  orgName: string;
  role: string;
}

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  refreshUser: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  const refreshUser = useCallback(async () => {
    const res = await apiFetch("/api/auth/me");
    if (res.ok) {
      const data = await res.json();
      setUser({
        id: data.id,
        email: data.email,
        orgId: data.org_id,
        orgName: data.org_name,
        role: data.role,
      });
    } else {
      setUser(null);
    }
    setLoading(false);
  }, []);

  // Mounted once, at the root layout — this is the "without re-fetching on
  // every page" fetch. Client-side navigations between pages don't remount
  // providers above the page level, so this state just persists.
  useEffect(() => {
    (async () => {
      await refreshUser();
    })();
  }, [refreshUser]);

  const logout = useCallback(async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, refreshUser, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
