"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "../lib/auth-context";
import LandingPage from "../components/landing/LandingPage";

export default function Home() {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && user) router.replace("/dashboard");
  }, [loading, user, router]);

  if (loading || user) {
    return (
      <div className="bg-grid flex flex-1 items-center justify-center bg-base">
        <p className="text-sm text-ink-tertiary">Loading…</p>
      </div>
    );
  }

  return <LandingPage />;
}
