import React, { act } from "react";
import { createRoot } from "react-dom/client";
import ErrorBoundary from "@/components/ErrorBoundary";

// A6: a render-time throw must be caught by the top-level ErrorBoundary and show a
// recoverable fallback instead of unmounting the whole SPA (white screen).

function Bomb() {
  throw new Error("boom");
}

describe("ErrorBoundary", () => {
  let container;
  let originalConsoleError;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    originalConsoleError = console.error;
    // The boundary logs caught errors via componentDidCatch; silence expected noise.
    console.error = jest.fn();
  });

  afterEach(() => {
    console.error = originalConsoleError;
    container.remove();
  });

  it("renders children normally when nothing throws", () => {
    const root = createRoot(container);
    act(() => {
      root.render(
        <ErrorBoundary>
          <div data-testid="ok">hello</div>
        </ErrorBoundary>,
      );
    });
    expect(container.textContent).toContain("hello");
    expect(container.textContent).not.toContain("Something went wrong");
    act(() => root.unmount());
  });

  it("catches a child render error and shows the recoverable fallback", () => {
    const root = createRoot(container);
    act(() => {
      root.render(
        <ErrorBoundary>
          <Bomb />
        </ErrorBoundary>,
      );
    });
    expect(container.textContent).toContain("Something went wrong");
    expect(container.querySelector("button")).not.toBeNull();
    act(() => root.unmount());
  });
});
