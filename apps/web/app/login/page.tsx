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
    <div className="flex flex-1 flex-col items-center justify-center gap-6 bg-zinc-50 font-sans dark:bg-black">
      <h1 className="text-2xl font-semibold tracking-tight text-black dark:text-zinc-50">
        Log in
      </h1>
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4 rounded-xl border border-black/[.08] p-6 dark:border-white/[.145]"
      >
        <label className="flex flex-col gap-1 text-sm">
          Email
          <input
            type="email"
            className="rounded-md border border-black/[.08] px-3 py-2 dark:border-white/[.145] dark:bg-zinc-900"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Password
          <input
            type="password"
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
          {submitting ? "Logging in..." : "Log in"}
        </button>
      </form>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        Need a workspace?{" "}
        <a href="/signup" className="font-medium text-black dark:text-zinc-50">
          Sign up
        </a>
      </p>
    </div>
  );
}
