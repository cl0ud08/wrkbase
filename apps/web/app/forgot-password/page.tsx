"use client";

import { useState } from "react";

import CopyableLink from "../../components/CopyableLink";

interface PasswordResetRequestResponse {
  message: string;
  // Dev-mode-only stand-in for real email delivery (out of scope) — see
  // apps/api/app/api/auth.py's request_password_reset. In production this
  // field wouldn't exist; the link would be emailed out-of-band instead.
  reset_link: string;
}

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<PasswordResetRequestResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    const res = await fetch("/api/auth/password-reset/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });

    if (res.ok) {
      setResult(await res.json());
    } else {
      // Rate-limited (429) is the one realistic failure here — the request
      // itself always "succeeds" from the caller's point of view otherwise,
      // by design (see the backend's enumeration-safety reasoning).
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Something went wrong. Please try again.");
    }
    setSubmitting(false);
  }

  return (
    <div className="bg-grid flex flex-1 flex-col items-center justify-center gap-6 bg-base px-4">
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 rounded-sm bg-accent shadow-[0_0_10px_var(--accent)]" aria-hidden="true" />
        <h1 className="text-xl font-bold tracking-tight text-ink">Reset your password</h1>
      </div>

      {!result && (
        <form
          onSubmit={handleSubmit}
          className="flex w-full max-w-sm flex-col gap-4 rounded-lg border border-line bg-surface p-6 shadow-[var(--shadow-elevated)]"
        >
          <p className="text-sm text-ink-secondary">
            Enter your email and, if there&apos;s an account for it, we&apos;ll generate a reset link.
          </p>
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
          {error && <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>}
          <button
            type="submit"
            disabled={submitting}
            className="rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
          >
            {submitting ? "Sending…" : "Send reset link"}
          </button>
        </form>
      )}

      {result && (
        <div className="flex w-full max-w-sm flex-col gap-3 rounded-lg border border-line bg-surface p-6 shadow-[var(--shadow-elevated)]">
          <p className="text-sm text-ink">{result.message}</p>
          <div className="flex flex-col gap-2 rounded-md border border-accent bg-accent-subtle p-3 text-sm">
            <p className="text-accent-subtle-text">
              Dev mode: email sending isn&apos;t wired up yet, so here&apos;s the link directly. This
              would never appear in a real response — it would only be emailed to the address above.
            </p>
            <CopyableLink link={result.reset_link} />
          </div>
          {/* Requesting again invalidates nothing on its own (each reset
              token is independently valid until used or expired — see
              request_password_reset), but the form was unreachable once
              `result` was set, with no way back except a full reload. This
              just gets back to it. */}
          <button
            type="button"
            onClick={() => setResult(null)}
            className="text-xs font-medium text-ink-tertiary hover:text-accent"
          >
            Send another link
          </button>
        </div>
      )}

      <p className="text-sm text-ink-secondary">
        <a href="/login" className="font-medium text-ink hover:text-accent">
          ← Back to log in
        </a>
      </p>
    </div>
  );
}
