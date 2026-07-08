"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

function VerifyEmailContent() {
  const token = useSearchParams().get("token");
  // No-token starts directly in the "error" state instead of flipping
  // there from inside the effect below — avoids a synchronous setState
  // in the effect body for a value already known at render time.
  const [state, setState] = useState<"verifying" | "success" | "error">(
    token ? "verifying" : "error",
  );
  const [error, setError] = useState<string | null>(
    token ? null : "This verification link is missing its token.",
  );

  useEffect(() => {
    if (!token) return;
    (async () => {
      const res = await fetch("/api/auth/verify-email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (res.ok) {
        setState("success");
      } else {
        const data = await res.json().catch(() => null);
        setState("error");
        setError(data?.detail ?? "This verification link is invalid, expired, or already used.");
      }
    })();
  }, [token]);

  if (state === "verifying") {
    return <p className="text-sm text-ink-tertiary">Verifying…</p>;
  }

  if (state === "success") {
    return (
      <div className="flex w-full max-w-sm flex-col items-center gap-3 text-center">
        <h1 className="text-xl font-bold tracking-tight text-ink">Email verified</h1>
        <p className="text-sm text-ink-secondary">You&apos;re all set.</p>
        <a
          href="/dashboard"
          className="rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
        >
          Go to dashboard
        </a>
      </div>
    );
  }

  return (
    <div className="flex w-full max-w-sm flex-col items-center gap-3 text-center">
      <h1 className="text-xl font-bold tracking-tight text-ink">Verification failed</h1>
      <p className="text-sm text-ink-secondary">{error}</p>
      <p className="text-xs text-ink-tertiary">
        Your account still works normally — this doesn&apos;t block you from using Wrkbase. You can
        request a new link from the banner once you&apos;re logged in.
      </p>
      <a href="/dashboard" className="text-sm font-medium text-ink hover:text-accent">
        Go to dashboard
      </a>
    </div>
  );
}

export default function VerifyEmailPage() {
  return (
    <div className="bg-grid flex flex-1 flex-col items-center justify-center gap-6 bg-base px-4">
      {/* useSearchParams requires a Suspense boundary in the App Router —
          same reason as signup/page.tsx's invite-token handling. */}
      <Suspense fallback={<p className="text-sm text-ink-tertiary">Loading…</p>}>
        <VerifyEmailContent />
      </Suspense>
    </div>
  );
}
