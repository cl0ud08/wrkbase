"use client";

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "../../../lib/api";
import { mapProject, type Project } from "../../../lib/types";

export default function DashboardPage() {
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
    <div className="flex flex-1 flex-col items-center px-4 py-10">
      <div className="flex w-full max-w-2xl flex-col gap-5">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-bold tracking-tight text-ink">Projects</h1>
          <button
            onClick={() => setShowForm((v) => !v)}
            className="rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
          >
            {showForm ? "Cancel" : "New project"}
          </button>
        </div>

        {showForm && (
          <form
            onSubmit={handleCreate}
            className="flex flex-col gap-3 rounded-lg border border-line bg-surface p-4"
          >
            <label className="flex flex-col gap-1.5 text-sm text-ink-secondary">
              Name
              <input
                className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </label>
            <label className="flex flex-col gap-1.5 text-sm text-ink-secondary">
              Description
              <textarea
                className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
              />
            </label>
            {error && (
              <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>
            )}
            <button
              type="submit"
              disabled={submitting}
              className="self-start rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
            >
              {submitting ? "Creating…" : "Create project"}
            </button>
          </form>
        )}

        {projects === null && <p className="text-sm text-ink-tertiary">Loading…</p>}

        {projects !== null && projects.length === 0 && (
          <div className="flex flex-col items-start gap-2.5 rounded-lg border border-dashed border-line px-5 py-9">
            <div className="flex h-8 w-8 items-center justify-center rounded-md border border-line bg-surface-2 font-mono text-sm text-accent">
              +
            </div>
            <h2 className="text-sm font-semibold text-ink">No projects yet</h2>
            <p className="max-w-xs text-sm text-ink-secondary">
              Create your first project to start tracking work for your team.
            </p>
            <button
              onClick={() => setShowForm(true)}
              className="mt-1 rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
            >
              New project
            </button>
          </div>
        )}

        {projects !== null && projects.length > 0 && (
          <div className="flex flex-col overflow-hidden rounded-lg border border-line">
            {projects.map((project, i) => (
              <a
                key={project.id}
                href={`/projects/${project.id}`}
                className={`flex items-center justify-between gap-4 bg-surface px-4 py-3 transition-colors duration-100 hover:bg-hover ${
                  i !== 0 ? "border-t border-line-subtle" : ""
                }`}
              >
                <div className="min-w-0">
                  <h2 className="truncate text-sm font-semibold text-ink">{project.name}</h2>
                  {project.description && (
                    <p className="mt-0.5 truncate text-xs text-ink-secondary">
                      {project.description}
                    </p>
                  )}
                </div>
                <span className="flex-shrink-0 font-mono text-xs text-ink-tertiary">→</span>
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
