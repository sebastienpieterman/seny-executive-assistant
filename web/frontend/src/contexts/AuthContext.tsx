/**
 * Authentication context for Seny.
 *
 * Reads JWT from localStorage on mount (same key as legacy frontend).
 * Provides login/logout/isAuthenticated to the entire app.
 * Checks setup_complete status on mount and exposes it for routing.
 */

import {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from "react";
import { api, TOKEN_KEY } from "@/lib/api";

interface AuthContextType {
  token: string | null;
  isAuthenticated: boolean;
  setupComplete: boolean;
  setupLoading: boolean;
  login: (token: string) => void;
  logout: () => void;
  refreshSetupStatus: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  // Initialize directly from localStorage (not in useEffect) so the first
  // render already knows if the user is authenticated. This prevents
  // ProtectedRoute from redirecting to /login before the token is read.
  const [token, setToken] = useState<string | null>(() =>
    localStorage.getItem(TOKEN_KEY)
  );

  // Default setupComplete to true to prevent flash of wizard for existing users.
  // setupLoading starts true — we check async before rendering protected routes.
  const [setupComplete, setSetupComplete] = useState(true);
  const [setupLoading, setSetupLoading] = useState(true);

  // Check setup status whenever token changes
  useEffect(() => {
    if (token) {
      setSetupLoading(true);
      api.get<{ setup_complete: boolean }>("/api/settings/profile")
        .then(data => {
          setSetupComplete(data.setup_complete ?? true);
        })
        .catch(() => {
          setSetupComplete(true); // Fail-open: assume complete on error
        })
        .finally(() => {
          setSetupLoading(false);
        });
    } else {
      setSetupComplete(true);
      setSetupLoading(false);
    }
  }, [token]);

  const refreshSetupStatus = useCallback(async () => {
    try {
      const data = await api.get<{ setup_complete: boolean }>("/api/settings/profile");
      setSetupComplete(data.setup_complete ?? true);
    } catch {
      setSetupComplete(true);
    }
  }, []);

  const login = useCallback((newToken: string) => {
    localStorage.setItem(TOKEN_KEY, newToken);
    setToken(newToken);
  }, []);

  const logout = useCallback(() => {
    // Fire-and-forget: call backend to blocklist the token
    // Uses raw fetch (not api client) to avoid 401 redirect race condition
    const currentToken = localStorage.getItem(TOKEN_KEY);
    if (currentToken) {
      fetch("/api/auth/logout", {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${currentToken}`,
          "Content-Type": "application/json",
        },
      }).catch(() => {
        // Silently ignore — local logout proceeds regardless
      });
    }

    // Immediately clear local state (don't await the API call)
    localStorage.removeItem(TOKEN_KEY);
    setToken(null);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        token,
        isAuthenticated: token !== null,
        setupComplete,
        setupLoading,
        login,
        logout,
        refreshSetupStatus,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextType {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
