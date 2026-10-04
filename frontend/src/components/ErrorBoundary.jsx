import React from "react";

/**
 * Top-level error boundary (production hardening).
 *
 * Without a boundary, any render-time throw anywhere in the tree unmounts the entire
 * React root and the user is left on a blank white screen with no way out — a serious
 * failure mode for a multi-tenant financial application. This catches errors in the
 * component tree below it, logs them, and shows a recoverable fallback (reload) instead
 * of crashing the whole SPA.
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
    this.handleReload = this.handleReload.bind(this);
  }

  static getDerivedStateFromError(error) {
    // Flip to the fallback UI. We intentionally do not expose the raw error to the DOM
    // (it may contain internal paths or tenant data); only a generic message is rendered.
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error("Unhandled UI error:", error, info && info.componentStack);
  }

  handleReload() {
    this.setState({ hasError: false, error: null });
    if (typeof window !== "undefined") {
      window.location.reload();
    }
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-background text-foreground p-6">
          <div className="max-w-md w-full rounded-lg border border-border bg-card p-6 shadow-sm text-center">
            <h1 className="text-lg font-semibold mb-2">Something went wrong</h1>
            <p className="text-sm text-muted-foreground mb-4">
              The application hit an unexpected error. Please reload the page. If the
              problem persists, contact your administrator.
            </p>
            <button
              type="button"
              onClick={this.handleReload}
              className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
            >
              Reload
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
