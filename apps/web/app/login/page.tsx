"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "../../lib/auth-context";

export default function LoginPage() {
  const router = useRouter();
  const { refreshUser } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ email, password }),
    });

    if (res.ok) {
      await refreshUser();
      router.push("/");
      return;
    }

    const data = await res.json().catch(() => null);
    setError(data?.detail ?? "Login failed. Please try again.");
    setSubmitting(false);
  }

  return (
    <div className="bg-grid flex flex-1 flex-col items-center justify-center gap-6 bg-base px-4">
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 rounded-sm bg-accent shadow-[0_0_10px_var(--accent)]" aria-hidden="true" />
        <h1 className="text-xl font-bold tracking-tight text-ink">Log in</h1>
      </div>
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4 rounded-lg border border-line bg-surface p-6 shadow-[var(--shadow-elevated)]"
      >
        <label className="flex flex-col gap-1.5 text-sm text-ink-secondary">
          Email
          <input
            type="email"
            className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm text-ink-secondary">
          <span className="flex items-center justify-between">
            Password
            <a href="/forgot-password" className="text-xs font-medium text-ink-tertiary hover:text-accent">
              Forgot password?
            </a>
          </span>
          <input
            type="password"
            className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {error && (
          <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>
        )}
        <button
          type="submit"
          disabled={submitting}
          className="rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
        >
          {submitting ? "Logging in…" : "Log in"}
        </button>
      </form>
      <p className="text-sm text-ink-secondary">
        Need a workspace?{" "}
        <a href="/signup" className="font-medium text-ink hover:text-accent">
          Sign up
        </a>
      </p>
    </div>
  );
}
