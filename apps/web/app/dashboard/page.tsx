"use client";

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth-context";
import { mapProject, type Project } from "../../lib/types";

export default function DashboardPage() {
  const { user } = useAuth();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const loadProjects = useCallback(async () => {
    const res = await apiFetch("/api/projects");
    if (res.ok) {
      const data = await res.json();
      setProjects(data.map(mapProject));
    }
  }, []);

  useEffect(() => {
    (async () => {
      await loadProjects();
    })();
  }, [loadProjects]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    const res = await apiFetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description: description || null }),
    });

    if (res.ok) {
      setName("");
      setDescription("");
      setShowForm(false);
      await loadProjects();
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to create project.");
    }
    setSubmitting(false);
  }

  return (
    <div className="flex flex-1 flex-col items-center gap-6 bg-zinc-50 px-6 py-16 font-sans dark:bg-black">
      <div className="flex w-full max-w-2xl items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-black dark:text-zinc-50">
            Projects
          </h1>
          {user && (
            <p className="text-sm text-zinc-500 dark:text-zinc-500">{user.orgName}</p>
          )}
        </div>
        <button
          onClick={() => setShowForm((v) => !v)}
          className="rounded-full bg-foreground px-5 py-2 text-sm font-medium text-background transition-colors hover:bg-[#383838] dark:hover:bg-[#ccc]"
        >
          {showForm ? "Cancel" : "New project"}
        </button>
      </div>

      {showForm && (
        <form
          onSubmit={handleCreate}
          className="flex w-full max-w-2xl flex-col gap-4 rounded-xl border border-black/[.08] p-6 dark:border-white/[.145]"
        >
          <label className="flex flex-col gap-1 text-sm">
            Name
            <input
              className="rounded-md border border-black/[.08] px-3 py-2 dark:border-white/[.145] dark:bg-zinc-900"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Description
            <textarea
              className="rounded-md border border-black/[.08] px-3 py-2 dark:border-white/[.145] dark:bg-zinc-900"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
            />
          </label>
          {error && <p className="text-sm text-red-500">{error}</p>}
          <button
            type="submit"
            disabled={submitting}
            className="self-start rounded-full bg-foreground px-5 py-2 text-sm font-medium text-background transition-colors hover:bg-[#383838] disabled:opacity-50 dark:hover:bg-[#ccc]"
          >
            {submitting ? "Creating..." : "Create project"}
          </button>
        </form>
      )}

      <div className="flex w-full max-w-2xl flex-col gap-3">
        {projects === null && (
          <p className="text-sm text-zinc-600 dark:text-zinc-400">Loading...</p>
        )}
        {projects !== null && projects.length === 0 && (
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            No projects yet — create your first one.
          </p>
        )}
        {projects?.map((project) => (
          <div
            key={project.id}
            className="rounded-xl border border-black/[.08] p-4 dark:border-white/[.145]"
          >
            <h2 className="font-medium text-black dark:text-zinc-50">{project.name}</h2>
            {project.description && (
              <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
                {project.description}
              </p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
