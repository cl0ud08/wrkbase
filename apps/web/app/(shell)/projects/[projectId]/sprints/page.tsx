"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { apiFetch } from "../../../../../lib/api";
import { useAuth } from "../../../../../lib/auth-context";
import { mapSprint, type Sprint, type SprintStatus } from "../../../../../lib/types";

const STATUS_CHIP: Record<SprintStatus, string> = {
  planned: "bg-neutral-bg text-neutral",
  active: "bg-info-bg text-info",
  completed: "bg-success-bg text-success",
};

function Dot() {
  return <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />;
}

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

function twoWeeksFromToday(): string {
  const d = new Date();
  d.setDate(d.getDate() + 14);
  return d.toISOString().slice(0, 10);
}

export default function SprintsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  const [sprints, setSprints] = useState<Sprint[] | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [startDate, setStartDate] = useState(todayISO());
  const [endDate, setEndDate] = useState(twoWeeksFromToday());
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    const res = await apiFetch(`/api/projects/${projectId}/sprints`);
    if (res.ok) setSprints((await res.json()).map(mapSprint));
  }, [projectId]);

  useEffect(() => {
    (async () => {
      await load();
    })();
  }, [load]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    const res = await apiFetch(`/api/projects/${projectId}/sprints`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        goal: goal || null,
        start_date: startDate,
        end_date: endDate,
      }),
    });

    if (res.ok) {
      setName("");
      setGoal("");
      setShowForm(false);
      await load();
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to create sprint.");
    }
    setSubmitting(false);
  }

  async function handleDelete(sprintId: string) {
    if (!confirm("Delete this sprint? This can't be undone.")) return;
    setError(null);
    const res = await apiFetch(`/api/projects/${projectId}/sprints/${sprintId}`, {
      method: "DELETE",
    });
    if (res.ok) {
      await load();
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to delete sprint.");
    }
  }

  const groups: { label: string; items: Sprint[] }[] = sprints
    ? [
        { label: "Active", items: sprints.filter((s) => s.status === "active") },
        { label: "Planned", items: sprints.filter((s) => s.status === "planned") },
        { label: "Completed", items: sprints.filter((s) => s.status === "completed") },
      ]
    : [];

  return (
    <div className="flex flex-1 flex-col items-center px-4 py-10">
      <div className="flex w-full max-w-2xl flex-col gap-5">
        <div>
          <a href={`/projects/${projectId}`} className="text-xs text-ink-tertiary hover:text-ink-secondary">
            ← Board
          </a>
          <div className="flex items-center justify-between gap-3">
            <h1 className="text-lg font-bold tracking-tight text-ink">Sprints</h1>
            {isAdmin && (
              <button
                onClick={() => setShowForm((v) => !v)}
                className="rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
              >
                {showForm ? "Cancel" : "New sprint"}
              </button>
            )}
          </div>
        </div>

        {error && <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>}

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
              Goal (optional)
              <textarea
                className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                rows={2}
              />
            </label>
            <div className="flex gap-3">
              <label className="flex flex-1 flex-col gap-1.5 text-sm text-ink-secondary">
                Start date
                <input
                  type="date"
                  className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
                  value={startDate}
                  onChange={(e) => setStartDate(e.target.value)}
                  required
                />
              </label>
              <label className="flex flex-1 flex-col gap-1.5 text-sm text-ink-secondary">
                End date
                <input
                  type="date"
                  className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
                  value={endDate}
                  onChange={(e) => setEndDate(e.target.value)}
                  required
                />
              </label>
            </div>
            <button
              type="submit"
              disabled={submitting}
              className="self-start rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
            >
              {submitting ? "Creating…" : "Create sprint"}
            </button>
          </form>
        )}

        {sprints === null && <p className="text-sm text-ink-tertiary">Loading…</p>}

        {sprints !== null && sprints.length === 0 && (
          <div className="flex flex-col items-start gap-2.5 rounded-lg border border-dashed border-line px-5 py-9">
            <div className="flex h-8 w-8 items-center justify-center rounded-md border border-line bg-surface-2 font-mono text-sm text-accent">
              +
            </div>
            <h2 className="text-sm font-semibold text-ink">No sprints yet</h2>
            <p className="max-w-xs text-sm text-ink-secondary">
              Create a sprint to start planning work out of the backlog.
            </p>
          </div>
        )}

        {groups.map(
          (group) =>
            group.items.length > 0 && (
              <div key={group.label} className="flex flex-col gap-2">
                <h2 className="text-sm font-semibold text-ink">{group.label}</h2>
                <div className="flex flex-col overflow-hidden rounded-lg border border-line">
                  {group.items.map((sprint, i) => (
                    <div
                      key={sprint.id}
                      className={`flex items-center justify-between gap-3 bg-surface px-4 py-3 ${
                        i !== 0 ? "border-t border-line-subtle" : ""
                      }`}
                    >
                      <a href={`/projects/${projectId}/sprints/${sprint.id}`} className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <h3 className="truncate text-sm font-semibold text-ink hover:text-accent">
                            {sprint.name}
                          </h3>
                          <span
                            className={`flex flex-shrink-0 items-center gap-1.5 rounded-sm px-2 py-0.5 text-xs font-medium ${STATUS_CHIP[sprint.status]}`}
                          >
                            <Dot />
                            {sprint.status}
                          </span>
                        </div>
                        <p className="mt-0.5 text-xs text-ink-tertiary">
                          {sprint.startDate} → {sprint.endDate} · {sprint.totalPoints} pts
                        </p>
                      </a>
                      {isAdmin && sprint.status === "planned" && (
                        <button
                          onClick={() => handleDelete(sprint.id)}
                          className="flex-shrink-0 rounded-md border border-line px-2.5 py-1 text-xs font-medium text-danger transition-colors duration-100 hover:border-danger hover:bg-danger-bg"
                        >
                          Delete
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ),
        )}
      </div>
    </div>
  );
}
