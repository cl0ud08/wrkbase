"use client";

import { useAuth } from "../../lib/auth-context";

// Placeholder protected page: exists so there's an actual gated route to
// prove the middleware redirect against (the homepage itself is public —
// it has to render for logged-out visitors too, see app/page.tsx). Projects
// CRUD, the next slice, replaces this.
export default function DashboardPage() {
  const { user } = useAuth();

  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-zinc-50 font-sans dark:bg-black">
      <h1 className="text-2xl font-semibold tracking-tight text-black dark:text-zinc-50">
        Dashboard
      </h1>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        {user ? `Signed in as ${user.email}` : "Loading..."} — Projects CRUD
        lands here next.
      </p>
    </div>
  );
}
