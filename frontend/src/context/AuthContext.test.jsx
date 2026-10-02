/**
 * Regression test for the F5-reload logout bug.
 *
 * Two defects combined to log a user out on page reload:
 *  (1) React.StrictMode (dev) double-invoked the rehydrate effect, replaying the
 *      same rotating refresh token; the second /auth/refresh 401'd and cleared auth.
 *      Fixed with a run-once ref guard (api.refresh must be called EXACTLY once).
 *  (2) /auth/refresh returns only tokens, but the handler trusted data.user/data.firm,
 *      leaving user undefined -> isAuthenticated false -> login screen. Fixed by
 *      hydrating the identity from /auth/me after refresh.
 *
 * NODE_ENV is forced off 'test' so the real rehydration branch runs, and the
 * component is wrapped in StrictMode to reproduce the double-invoke condition.
 * mockRefreshImpl intentionally returns NO user/firm (real backend contract), so
 * the test fails unless getMe hydration is used.
 */
import React, { StrictMode, act } from "react";
import { createRoot } from "react-dom/client";

let mockRefreshImpl;
jest.mock("@/lib/api", () => ({
  api: {
    refresh: (...a) => mockRefreshImpl(...a),
    logout: jest.fn(),
    getMe: jest.fn(),
  },
  setAccessToken: jest.fn(),
  setRefreshToken: jest.fn(),
  getRefreshToken: () => "stored-refresh-token",
  setOnUnauthorized: jest.fn(),
}));

import { AuthProvider, useAuth } from "@/context/AuthContext";
import { api } from "@/lib/api";

let container, root;
const prevEnv = process.env.NODE_ENV;
let latest;
function Probe() { latest = useAuth(); return null; }

beforeEach(() => {
  process.env.NODE_ENV = "development"; // exercise the real rehydration branch
  // Real refresh contract: tokens only, NO user/firm.
  mockRefreshImpl = jest.fn(async () => ({ access_token: "new-access", refresh_token: "rotated" }));
  api.getMe = jest.fn(async () => ({ user: { id: "u1" }, firm: { id: "f1" } }));
  container = document.createElement("div"); document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount()); container.remove(); process.env.NODE_ENV = prevEnv;
});

test("reload rehydrates session: refresh once + identity from /auth/me, stays authenticated", async () => {
  await act(async () => {
    root.render(
      <StrictMode>
        <AuthProvider><Probe /></AuthProvider>
      </StrictMode>
    );
  });
  for (let i = 0; i < 4; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });

  expect(mockRefreshImpl.mock.calls.length).toBe(1); // DEFECT 1 guard: no double-rotate
  expect(api.getMe.mock.calls.length).toBe(1);        // DEFECT 2 guard: hydrate identity
  expect(latest.isAuthenticated).toBe(true);          // session actually restored
});
