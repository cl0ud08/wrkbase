"use client";

import { useState } from "react";

// Soft-nudge, not a gate — see apps/api/app/db/models.py's
// User.is_verified docstring. Shown on every page while unverified
// (ShellLayout only renders this when user && !user.isVerified), never
// blocking anything underneath it.
export default function VerificationBanner({ email }: { email: string }) {
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const [link, setLink] = useState<string | null>(null);

  async function handleResend() {
    setState("sending");
    const res = await fetch("/api/auth/resend-verification", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    const data = await res.json().catch(() => null);
    setLink(data?.verification_link ?? null);
    setState("sent");
  }

  return (
    <div className="flex flex-col gap-1 border-b border-line-subtle bg-info-bg px-4 py-2 text-xs sm:flex-row sm:items-center sm:justify-between">
      <span className="text-info">
        Your email isn&apos;t verified yet. Wrkbase works fine either way — verifying just makes sure
        we can reach you for things like password resets.
      </span>
      {state !== "sent" && (
        <button
          onClick={handleResend}
          disabled={state === "sending"}
          className="flex-shrink-0 text-left font-medium text-info underline decoration-dotted disabled:opacity-50 sm:text-right"
        >
          {state === "sending" ? "Sending…" : "Resend verification email"}
        </button>
      )}
      {state === "sent" && link && (
        <a
          href={link}
          className="flex-shrink-0 truncate font-mono text-info underline decoration-dotted sm:max-w-xs"
        >
          Dev mode: {link}
        </a>
      )}
    </div>
  );
}
