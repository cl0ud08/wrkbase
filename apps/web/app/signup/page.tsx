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
      await refreshUser();
      router.push("/");
      return;
    }

    const data = await res.json().catch(() => null);
    setError(data?.detail ?? "Signup failed. Please try again.");
    setSubmitting(false);
  }

  if (preview === "loading") {
    return <p className="text-sm text-zinc-600 dark:text-zinc-400">Checking invite...</p>;
  }

  if (preview === "invalid") {
    return (
      <div className="flex w-full max-w-sm flex-col items-center gap-4 text-center">
        <h1 className="text-2xl font-semibold tracking-tight text-black dark:text-zinc-50">
          Invite link invalid
        </h1>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          This invite link is invalid, expired, or has already been used. Ask whoever invited you
          to send a new one, or create your own workspace instead.
        </p>
        <a
          href="/signup"
          className="rounded-full bg-foreground px-5 py-2 text-sm font-medium text-background transition-colors hover:bg-[#383838] dark:hover:bg-[#ccc]"
        >
          Create a workspace
        </a>
      </div>
    );
  }

  return (
    <>
      <h1 className="text-2xl font-semibold tracking-tight text-black dark:text-zinc-50">
        {preview ? `You've been invited to join ${preview.org_name}` : "Create your workspace"}
      </h1>
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4 rounded-xl border border-black/[.08] p-6 dark:border-white/[.145]"
      >
        {!preview && (
          <label className="flex flex-col gap-1 text-sm">
            Organization name
            <input
              className="rounded-md border border-black/[.08] px-3 py-2 dark:border-white/[.145] dark:bg-zinc-900"
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
              required
            />
          </label>
        )}
        <label className="flex flex-col gap-1 text-sm">
          Email
          <input
            type="email"
            disabled={!!preview}
            className="rounded-md border border-black/[.08] px-3 py-2 disabled:opacity-60 dark:border-white/[.145] dark:bg-zinc-900"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Password
          <input
            type="password"
            minLength={8}
            className="rounded-md border border-black/[.08] px-3 py-2 dark:border-white/[.145] dark:bg-zinc-900"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {error && <p className="text-sm text-red-500">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="rounded-full bg-foreground px-5 py-2 text-sm font-medium text-background transition-colors hover:bg-[#383838] disabled:opacity-50 dark:hover:bg-[#ccc]"
        >
          {submitting ? "Creating account..." : preview ? "Join workspace" : "Sign up"}
        </button>
      </form>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        Already have a workspace?{" "}
        <a href="/login" className="font-medium text-black dark:text-zinc-50">
          Log in
        </a>
      </p>
    </>
  );
}

export default function SignupPage() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-6 bg-zinc-50 font-sans dark:bg-black">
      {/* useSearchParams requires a Suspense boundary in the App Router —
          without it, the page opts the whole route out of static
          rendering with a build-time warning. */}
      <Suspense fallback={<p className="text-sm text-zinc-600 dark:text-zinc-400">Loading...</p>}>
        <SignupForm />
      </Suspense>
    </div>
  );
}
