"use client";

import { useRouter } from "next/navigation";

import { useAuth } from "../lib/auth-context";

export default function Home() {
  const { user, loading, logout } = useAuth();
  const router = useRouter();

  async function handleLogout() {
    await logout();
    router.push("/login");
  }

  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-6 bg-zinc-50 font-sans dark:bg-black">
      <h1 className="text-3xl font-semibold tracking-tight text-black dark:text-zinc-50">
        Wrkbase
      </h1>

      {loading && (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">Loading...</p>
      )}

      {!loading && user && (
        <div className="flex flex-col items-center gap-3">
          <p className="text-sm text-zinc-700 dark:text-zinc-300">
            Signed in as <span className="font-medium">{user.email}</span> —{" "}
            {user.orgName}
          </p>
          <div className="flex gap-3">
            <a
              href="/dashboard"
              className="rounded-full bg-foreground px-5 py-2 text-sm font-medium text-background transition-colors hover:bg-[#383838] dark:hover:bg-[#ccc]"
            >
              Go to dashboard
            </a>
            <button
              onClick={handleLogout}
              className="rounded-full border border-black/[.08] px-5 py-2 text-sm font-medium transition-colors hover:bg-black/[.04] dark:border-white/[.145] dark:hover:bg-[#1a1a1a]"
            >
              Log out
            </button>
          </div>
        </div>
      )}

      {!loading && !user && (
        <div className="flex gap-3">
          <a
            href="/signup"
            className="rounded-full bg-foreground px-5 py-2 text-sm font-medium text-background transition-colors hover:bg-[#383838] dark:hover:bg-[#ccc]"
          >
            Sign up
          </a>
          <a
            href="/login"
            className="rounded-full border border-black/[.08] px-5 py-2 text-sm font-medium transition-colors hover:bg-black/[.04] dark:border-white/[.145] dark:hover:bg-[#1a1a1a]"
          >
            Log in
          </a>
        </div>
      )}
    </div>
  );
}
