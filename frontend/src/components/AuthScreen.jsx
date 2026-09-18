import React, { useState } from "react";
import { useAuth } from "@/context/AuthContext";
import { ShieldCheck, LogIn, UserPlus } from "lucide-react";

export default function AuthScreen() {
  const { login, signup, isLoading, error: contextError } = useAuth();
  const [isSignup, setIsSignup] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [firmName, setFirmName] = useState("");
  const [name, setName] = useState("");
  const [localError, setLocalError] = useState("");

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLocalError("");
    try {
      if (isSignup) {
        if (!firmName.trim()) {
          setLocalError("Firm name is required.");
          return;
        }
        await signup(email, password, firmName, name);
      } else {
        await login(email, password);
      }
    } catch (err) {
      setLocalError(err.message || "Authentication failed.");
    }
  };

  const errorMessage = localError || contextError;

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4 py-12">
      <div className="max-w-md w-full space-y-8 bg-card border border-border p-8 rounded-xl shadow-lg">
        <div className="text-center">
          <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-primary/10 text-primary mb-3">
            <ShieldCheck className="w-7 h-7" />
          </div>
          <h2 className="text-2xl font-bold tracking-tight text-foreground">
            LedgerLens Cloud
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {isSignup
              ? "Register your accounting firm and root administrator"
              : "Sign in to your firm's isolated tenant workspace"}
          </p>
        </div>

        {errorMessage && (
          <div
            data-testid="auth-error"
            className="p-3 rounded-md bg-destructive/15 border border-destructive/30 text-destructive text-sm"
          >
            {errorMessage}
          </div>
        )}

        <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
          {isSignup && (
            <>
              <div>
                <label className="block text-xs font-medium text-foreground mb-1">
                  Firm Name *
                </label>
                <input
                  data-testid="signup-firm"
                  type="text"
                  required
                  value={firmName}
                  onChange={(e) => setFirmName(e.target.value)}
                  placeholder="e.g. Apex Chartered Accountants"
                  className="w-full px-3 py-2 text-sm border border-input rounded-md bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-foreground mb-1">
                  Your Full Name
                </label>
                <input
                  data-testid="signup-name"
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. Jane Doe, CPA"
                  className="w-full px-3 py-2 text-sm border border-input rounded-md bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                />
              </div>
            </>
          )}

          <div>
            <label className="block text-xs font-medium text-foreground mb-1">
              Email Address *
            </label>
            <input
              data-testid={isSignup ? "signup-email" : "login-email"}
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="accountant@firm.com"
              className="w-full px-3 py-2 text-sm border border-input rounded-md bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-foreground mb-1">
              Password *
            </label>
            <input
              data-testid={isSignup ? "signup-password" : "login-password"}
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              minLength={isSignup ? 8 : 1}
              className="w-full px-3 py-2 text-sm border border-input rounded-md bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
            />
          </div>

          <button
            data-testid={isSignup ? "signup-submit" : "login-submit"}
            type="submit"
            disabled={isLoading}
            className="w-full flex justify-center items-center gap-2 py-2.5 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-primary-foreground bg-primary hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary disabled:opacity-50"
          >
            {isSignup ? (
              <>
                <UserPlus className="w-4 h-4" />
                {isLoading ? "Registering..." : "Create Firm Account"}
              </>
            ) : (
              <>
                <LogIn className="w-4 h-4" />
                {isLoading ? "Signing in..." : "Sign In"}
              </>
            )}
          </button>
        </form>

        <div className="pt-2 text-center border-t border-border">
          <button
            type="button"
            onClick={() => {
              setIsSignup(!isSignup);
              setLocalError("");
            }}
            className="text-xs text-primary hover:underline"
          >
            {isSignup
              ? "Already have a firm workspace? Sign in instead."
              : "Need a new firm workspace? Register for free."}
          </button>
        </div>
      </div>
    </div>
  );
}
