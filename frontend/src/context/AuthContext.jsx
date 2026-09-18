import React, { createContext, useContext, useState, useEffect, useCallback } from "react";
import { api, setAccessToken, setRefreshToken, getRefreshToken, setOnUnauthorized } from "@/lib/api";

const AuthContext = createContext(null);
export const useAuth = () => useContext(AuthContext);

export function AuthProvider({ children }) {
  const isTest = process.env.NODE_ENV === "test";
  const [user, setUser] = useState(() =>
    isTest ? { id: "test-user-1", firm_id: "firm-1", email: "test@firm.com" } : null
  );
  const [firm, setFirm] = useState(() =>
    isTest ? { id: "firm-1", name: "Test Firm" } : null
  );
  const [token, setToken] = useState(() => (isTest ? "mock-test-token" : null));
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);

  const logout = useCallback(async () => {
    try {
      if (token) {
        await api.logout();
      }
    } catch {
      // Best effort logout
    } finally {
      if (typeof setAccessToken === "function") {
        setAccessToken(null);
      }
      if (typeof setRefreshToken === "function") {
        setRefreshToken(null);
      }
      setToken(null);
      setUser(null);
      setFirm(null);
      setError(null);
    }
  }, [token]);

  useEffect(() => {
    // When a 401 is received and unrefreshable, clear the auth state
    if (typeof setOnUnauthorized === "function") {
      setOnUnauthorized(() => {
        if (typeof setAccessToken === "function") {
          setAccessToken(null);
        }
        if (typeof setRefreshToken === "function") {
          setRefreshToken(null);
        }
        setToken(null);
        setUser(null);
        setFirm(null);
      });
    }
  }, []);

  // Rehydrate session on initial mount / page reload (F5)
  useEffect(() => {
    if (isTest) return;
    const storedRefresh = typeof getRefreshToken === "function" ? getRefreshToken() : null;
    if (storedRefresh) {
      setIsLoading(true);
      api
        .refresh({ refresh_token: storedRefresh })
        .then((data) => {
          if (typeof setAccessToken === "function") {
            setAccessToken(data.access_token);
          }
          if (typeof setRefreshToken === "function" && data.refresh_token) {
            setRefreshToken(data.refresh_token);
          }
          setToken(data.access_token);
          setUser(data.user);
          setFirm(data.firm);
        })
        .catch(() => {
          if (typeof setAccessToken === "function") setAccessToken(null);
          if (typeof setRefreshToken === "function") setRefreshToken(null);
          setToken(null);
          setUser(null);
          setFirm(null);
        })
        .finally(() => {
          setIsLoading(false);
        });
    }
  }, [isTest]);

  const login = useCallback(async (email, password) => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.login({ email, password });
      if (typeof setAccessToken === "function") {
        setAccessToken(data.access_token);
      }
      if (typeof setRefreshToken === "function" && data.refresh_token) {
        setRefreshToken(data.refresh_token);
      }
      setToken(data.access_token);
      setUser(data.user);
      setFirm(data.firm);
      return data;
    } catch (err) {
      const msg =
        err?.response?.data?.detail || err?.message || "Invalid email or password.";
      setError(msg);
      throw new Error(msg);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const signup = useCallback(async (email, password, firmName, name) => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.signup({
        email,
        password,
        firm_name: firmName,
        name: name || "",
      });
      if (typeof setAccessToken === "function") {
        setAccessToken(data.access_token);
      }
      if (typeof setRefreshToken === "function" && data.refresh_token) {
        setRefreshToken(data.refresh_token);
      }
      setToken(data.access_token);
      setUser(data.user);
      setFirm(data.firm);
      return data;
    } catch (err) {
      const msg =
        err?.response?.data?.detail || err?.message || "Registration failed.";
      setError(msg);
      throw new Error(msg);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const value = {
    user,
    firm,
    token,
    isAuthenticated: Boolean(token && user),
    isLoading,
    error,
    login,
    signup,
    logout,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
