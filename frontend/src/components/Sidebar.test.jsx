/**
 * Regression test for the mobile navigation drawer.
 *
 * Guards:
 *  - the sidebar exposes an open/closed state (data-open) and a backdrop only when open
 *  - tapping a nav item both switches the tab AND closes the drawer (so mobile
 *    users aren't left staring at an overlay covering the page)
 *  - desktop (closed prop) still renders the full nav
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";

let mockSetTab, mockOnClose;
jest.mock("@/context/AppContext", () => ({
  useApp: () => ({
    tab: "dashboard",
    setTab: (...a) => mockSetTab(...a),
    activeClient: { id: "c1", name: "Acme" },
  }),
}));

import Sidebar from "@/components/Sidebar";

let container, root;
beforeEach(() => {
  mockSetTab = jest.fn();
  mockOnClose = jest.fn();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test("closed drawer: no backdrop, data-open=false, nav present", () => {
  act(() => { root.render(<Sidebar open={false} onClose={mockOnClose} />); });
  expect(container.querySelector('[data-testid="sidebar"]').getAttribute("data-open")).toBe("false");
  expect(container.querySelector('[data-testid="nav-backdrop"]')).toBeNull();
  expect(container.querySelector('[data-testid="nav-clients"]')).toBeTruthy();
});

test("open drawer: backdrop shown, data-open=true", () => {
  act(() => { root.render(<Sidebar open={true} onClose={mockOnClose} />); });
  expect(container.querySelector('[data-testid="sidebar"]').getAttribute("data-open")).toBe("true");
  expect(container.querySelector('[data-testid="nav-backdrop"]')).toBeTruthy();
});

test("tapping a nav item switches tab AND closes the drawer", () => {
  act(() => { root.render(<Sidebar open={true} onClose={mockOnClose} />); });
  act(() => { container.querySelector('[data-testid="nav-clients"]').dispatchEvent(new MouseEvent("click", { bubbles: true })); });
  expect(mockSetTab).toHaveBeenCalledWith("clients");
  expect(mockOnClose).toHaveBeenCalled();
});
