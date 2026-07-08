"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { useAuth } from "../../lib/auth-context";

interface InvitePreview {
  org_name: string;
  email: string;
  role: string;
}

function SignupForm() {
  const router = useRouter();
  const { refreshUser } = useAuth();
  const inviteToken = useSearchParams().get("invite");

  const [orgName, setOrgName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // Set only for a self-serve signup (see apps/api/app/api/auth.py's
  // signup — null for an invite redemption, which is auto-verified and
  // has nothing to confirm). Holding this back from an immediate redirect
  // is what actually shows the "check your email" state below; joining
  // via invite skips straight to the dashboard, same as before this slice.
  const [verificationLink, setVerificationLink] = useState<string | null>(null);

  // null = no token / not checked yet, "loading", a preview, or "invalid".
  const [preview, setPreview] = useState<InvitePreview | "loading" | "invalid" | null>(
    inviteToken ? "loading" : null,
  );

  useEffect(() => {
    if (!inviteToken) return;
    (async () => {
      const res = await fetch(`/api/invites/preview?token=${encodeURIComponent(inviteToken)}`);
      if (res.ok) {
        const data: InvitePreview = await res.json();
        setPreview(data);
        setEmail(data.email);
      } else {
        setPreview("invalid");
      }
    })();
  }, [inviteToken]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    const body = inviteToken
      ? { invite_token: inviteToken, email, password }
      : { org_name: orgName, email, password };

    const res = await fetch("/api/auth/signup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
    });

    if (res.ok) {
      const data = await res.json();
      if (data.verification_link) {
        // Self-serve signup: hold here and show the confirmation state
        // below instead of redirecting immediately. Not a gate — the
        // account is already fully usable — just a moment to surface the
        // dev-mode link before moving on.
        setVerificationLink(data.verification_link);
        setSubmitting(false);
        return;
      }
      await refreshUser();
      router.push("/dashboard");
      return;
    }

    const data = await res.json().catch(() => null);
    setError(data?.detail ?? "Signup failed. Please try again.");
    setSubmitting(false);
  }

  async function handleContinue() {
    await refreshUser();
    router.push("/dashboard");
  }

  if (verificationLink) {
    return (
      <div className="flex w-full max-w-sm flex-col items-center gap-4 text-center">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-sm bg-accent shadow-[0_0_10px_var(--accent)]" aria-hidden="true" />
          <h1 className="text-xl font-bold tracking-tight text-ink">Check your email</h1>
        </div>
        <p className="text-sm text-ink-secondary">
          Your workspace is ready to use right away — verifying just makes sure we can reach you for
          things like password resets later.
        </p>
        <div className="w-full rounded-md border border-accent bg-accent-subtle p-3 text-left text-sm">
          <p className="text-accent-subtle-text">
            Dev mode: email sending isn&apos;t wired up yet, so here&apos;s the link directly.
          </p>
          <a
            href={verificationLink}
            className="mt-1 block truncate font-mono text-xs text-ink underline decoration-dotted"
          >
            {verificationLink}
          </a>
        </div>
        <button
          onClick={handleContinue}
          className="rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
        >
          Continue to dashboard
        </button>
      </div>
    );
  }

  if (preview === "loading") {
    return <p className="text-sm text-ink-tertiary">Checking invite…</p>;
  }

  if (preview === "invalid") {
    return (
      <div className="flex w-full max-w-sm flex-col items-center gap-4 text-center">
        <h1 className="text-xl font-bold tracking-tight text-ink">Invite link invalid</h1>
        <p className="text-sm text-ink-secondary">
          This invite link is invalid, expired, or has already been used. Ask whoever invited you
          to send a new one, or create your own workspace instead.
        </p>
        <a
          href="/signup"
          className="rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
        >
          Create a workspace
        </a>
      </div>
    );
  }

  return (
    <>
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 rounded-sm bg-accent shadow-[0_0_10px_var(--accent)]" aria-hidden="true" />
        <h1 className="text-xl font-bold tracking-tight text-ink">
          {preview ? `You've been invited to join ${preview.org_name}` : "Create your workspace"}
        </h1>
      </div>
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4 rounded-lg border border-line bg-surface p-6 shadow-[var(--shadow-elevated)]"
      >
        {!preview && (
          <label className="flex flex-col gap-1.5 text-sm text-ink-secondary">
            Organization name
            <input
              className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
              required
            />
          </label>
        )}
        <label className="flex flex-col gap-1.5 text-sm text-ink-secondary">
          Email
          <input
            type="email"
            disabled={!!preview}
            className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong disabled:opacity-60"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm text-ink-secondary">
          Password
          <input
            type="password"
            minLength={8}
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
          {submitting ? "Creating account…" : preview ? "Join workspace" : "Sign up"}
        </button>
      </form>
      <p className="text-sm text-ink-secondary">
        Already have a workspace?{" "}
        <a href="/login" className="font-medium text-ink hover:text-accent">
          Log in
        </a>
      </p>
    </>
  );
}

export default function SignupPage() {
  return (
    <div className="bg-grid flex flex-1 flex-col items-center justify-center gap-6 bg-base px-4">
      {/* useSearchParams requires a Suspense boundary in the App Router —
          without it, the page opts the whole route out of static
          rendering with a build-time warning. */}
      <Suspense fallback={<p className="text-sm text-ink-tertiary">Loading…</p>}>
        <SignupForm />
      </Suspense>
    </div>
  );
}
