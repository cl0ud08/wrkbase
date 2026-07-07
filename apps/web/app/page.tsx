"use client";

import { useEffect, useState } from "react";

type HealthState =
  | { status: "loading" }
  | { status: "ok"; service: string }
  | { status: "error"; message: string };

export default function Home() {
  const [health, setHealth] = useState<HealthState>({ status: "loading" });

  useEffect(() => {
    const apiUrl = process.env.NEXT_PUBLIC_API_URL;

    fetch(`${apiUrl}/health`)
      .then((res) => {
        if (!res.ok) throw new Error(`API responded with ${res.status}`);
        return res.json();
      })
      .then((data) => setHealth({ status: "ok", service: data.service }))
      .catch((err) =>
        setHealth({ status: "error", message: err.message as string }),
      );
  }, []);

  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-6 bg-zinc-50 font-sans dark:bg-black">
      <h1 className="text-3xl font-semibold tracking-tight text-black dark:text-zinc-50">
        Wrkbase
      </h1>
      <div className="flex items-center gap-2 rounded-full border border-black/[.08] px-4 py-2 text-sm dark:border-white/[.145]">
        <span
          className={`h-2 w-2 rounded-full ${
            health.status === "ok"
              ? "bg-green-500"
              : health.status === "error"
                ? "bg-red-500"
                : "bg-zinc-400 animate-pulse"
          }`}
        />
        {health.status === "loading" && <span>Checking API...</span>}
        {health.status === "ok" && <span>API is up ({health.service})</span>}
        {health.status === "error" && (
          <span>API unreachable: {health.message}</span>
        )}
      </div>
    </div>
  );
}
