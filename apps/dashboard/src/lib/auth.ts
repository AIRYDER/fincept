/**
 * Auth: localStorage-backed JWT for v1.
 *
 * The API uses HS256 JWT bearer tokens.  In dev, the operator pastes a
 * token they minted via ``api.auth.encode_token`` (or the mint-token
 * helper in scripts/).  Phase H replaces this with a real OAuth flow
 * + httpOnly cookies.
 *
 * Zustand provides a global, hook-friendly observable so any component
 * can react to login/logout without prop drilling.
 */

"use client";

import { create } from "zustand";

const TOKEN_KEY = "fincept.token";

interface AuthState {
  token: string | null;
  setToken: (token: string | null) => void;
  hydrate: () => void;
}

export const useAuth = create<AuthState>((set) => ({
  token: null,
  setToken: (token) => {
    if (typeof window !== "undefined") {
      if (token) localStorage.setItem(TOKEN_KEY, token);
      else localStorage.removeItem(TOKEN_KEY);
    }
    set({ token });
  },
  hydrate: () => {
    if (typeof window === "undefined") return;
    const stored = localStorage.getItem(TOKEN_KEY);
    if (stored) set({ token: stored });
  },
}));

export function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function decodeJwt(token: string): Record<string, unknown> | null {
  try {
    const [, payload] = token.split(".");
    if (!payload) return null;
    const decoded = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(decoded);
  } catch {
    return null;
  }
}
