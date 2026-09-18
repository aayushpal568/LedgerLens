import React, { act } from "react";
import { createRoot } from "react-dom/client";
import App from "@/App";
import AuthScreen from "@/components/AuthScreen";
import { AuthProvider, useAuth } from "@/context/AuthContext";

// Mock the API layer
jest.mock("./lib/api", () => {
  let _token = null;
  let _unauthCb = null;
  return {
    __esModule: true,
    API: "http://127.0.0.1:8001/api",
    setAccessToken: jest.fn((t) => { _token = t; }),
    getAccessToken: jest.fn(() => _token),
    setOnUnauthorized: jest.fn((cb) => { _unauthCb = cb; }),
    api: {
      login: jest.fn().mockImplementation(({ email, password }) => {
        if (password === "wrongpass") {
          const err = new Error("Invalid email or password");
          err.response = { status: 401, data: { detail: "Invalid email or password" } };
          return Promise.reject(err);
        }
        return Promise.resolve({
          access_token: "mock-access-token-123",
          user: { id: "u-1", email, firm_id: "f-1" },
          firm: { id: "f-1", name: "Test Firm" },
        });
      }),
      signup: jest.fn().mockImplementation(({ email, password, firm_name, name }) => {
        if (email === "duplicate@firm.com") {
          const err = new Error("An account with this email address already exists.");
          err.response = { status: 409, data: { detail: "An account with this email address already exists." } };
          return Promise.reject(err);
        }
        return Promise.resolve({
          access_token: "mock-signup-token-456",
          user: { id: "u-2", email, firm_id: "f-2", name },
          firm: { id: "f-2", name: firm_name },
        });
      }),
      logout: jest.fn().mockResolvedValue({ ok: true }),
      getFirm: jest.fn().mockResolvedValue({ id: "f-1", name: "Test Firm" }),
      listClients: jest.fn().mockResolvedValue([]),
    },
  };
});

let container = null;
let root = null;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  if (root) {
    await act(async () => {
      root.unmount();
    });
  }
  if (container) {
    container.remove();
    container = null;
  }
});

describe("Frontend Authentication & Tenant Context Verification", () => {
  test("1. AuthScreen renders login form with exact test IDs", async () => {
    await act(async () => {
      root.render(
        <AuthProvider>
          <AuthScreen />
        </AuthProvider>
      );
    });

    expect(container.querySelector("[data-testid='login-email']")).not.toBeNull();
    expect(container.querySelector("[data-testid='login-password']")).not.toBeNull();
    expect(container.querySelector("[data-testid='login-submit']")).not.toBeNull();
  });

  test("2. Toggling to signup renders signup form with exact test IDs", async () => {
    await act(async () => {
      root.render(
        <AuthProvider>
          <AuthScreen />
        </AuthProvider>
      );
    });

    const toggleBtn = container.querySelector("button[type='button']");
    expect(toggleBtn).not.toBeNull();

    await act(async () => {
      toggleBtn.click();
    });

    expect(container.querySelector("[data-testid='signup-firm']")).not.toBeNull();
    expect(container.querySelector("[data-testid='signup-name']")).not.toBeNull();
    expect(container.querySelector("[data-testid='signup-email']")).not.toBeNull();
    expect(container.querySelector("[data-testid='signup-password']")).not.toBeNull();
    expect(container.querySelector("[data-testid='signup-submit']")).not.toBeNull();
  });
});
