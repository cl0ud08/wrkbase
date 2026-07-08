"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

function ResetPasswordForm() {
  const router = useRouter();
  const token = useSearchParams().get("token");

  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (password !== confirmPassword) {
      setError("Passwords don't match.");
      return;
    }
    if (!token) {
      setError("This reset link is missing its token.");
      return;
    }

    setSubmitting(true);
    const res = await fetch("/api/auth/password-reset/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, new_password: password }),
    });

    if (res.ok) {
      setDone(true);
      // Every existing session (including this browser's, if it happened
      // to be logged in) was just revoked server-side — sending them to
      // log in fresh with the new password, not back into the app, is the
      // correct next step, not just a UX default.
      setTimeout(() => router.push("/login"), 2500);
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "This reset link is invalid, expired, or already used.");
    }
    setSubmitting(false);
  }

  if (!token) {
    return (
      <div className="flex w-full max-w-sm flex-col items-center gap-4 text-center">
        <h1 className="text-xl font-bold tracking-tight text-ink">Reset link invalid</h1>
        <p className="text-sm text-ink-secondary">
          This link is missing its token. Request a new one instead.
        </p>
        <a
          href="/forgot-password"
          className="rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
        >
          Request a new link
        </a>
      </div>
    );
  }

  if (done) {
    return (
      <div className="flex w-full max-w-sm flex-col items-center gap-3 text-center">
        <h1 className="text-xl font-bold tracking-tight text-ink">Password reset</h1>
        <p className="text-sm text-ink-secondary">
          Every device you were signed in on has been signed out. Redirecting you to log in with
          your new password…
        </p>
      </div>
    );
  }

  return (
    <>
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 rounded-sm bg-accent shadow-[0_0_10px_var(--accent)]" aria-hidden="true" />
        <h1 className="text-xl font-bold tracking-tight text-ink">Choose a new password</h1>
      </div>
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4 rounded-lg border border-line bg-surface p-6 shadow-[var(--shadow-elevated)]"
      >
        <label className="flex flex-col gap-1.5 text-sm text-ink-secondary">
          New password
          <input
            type="password"
            minLength={8}
            className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm text-ink-secondary">
          Confirm new password
          <input
            type="password"
            minLength={8}
            className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
          />
        </label>
        {error && <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
        >
          {submitting ? "Resetting…" : "Reset password"}
        </button>
      </form>
    </>
  );
}

export default function ResetPasswordPage() {
  return (
    <div className="bg-grid flex flex-1 flex-col items-center justify-center gap-6 bg-base px-4">
      {/* useSearchParams requires a Suspense boundary in the App Router —
          same reason as signup/page.tsx's invite-token handling. */}
      <Suspense fallback={<p className="text-sm text-ink-tertiary">Loading…</p>}>
        <ResetPasswordForm />
      </Suspense>
    </div>
  );
}
