"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { apiFetch } from "../../../../../lib/api";
import { mapWorkflowState, type WorkflowState } from "../../../../../lib/types";

export default function WorkflowSettingsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const [states, setStates] = useState<WorkflowState[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const res = await apiFetch(`/api/projects/${projectId}/workflow-states`);
    if (res.ok) {
      const data = await res.json();
      setStates(
        data.map(mapWorkflowState).sort((a: WorkflowState, b: WorkflowState) => a.order - b.order),
      );
    }
  }, [projectId]);

  useEffect(() => {
    (async () => {
      await load();
    })();
  }, [load]);

  // Basic, deliberately: swap two `order` values via two PATCH calls
  // rather than wiring dnd-kit here too — this view just needs to work,
  // not match the board's drag interaction.
  async function swap(index: number, direction: -1 | 1) {
    if (!states) return;
    const otherIndex = index + direction;
    if (otherIndex < 0 || otherIndex >= states.length) return;
    const a = states[index];
    const b = states[otherIndex];

    setError(null);
    const [resA, resB] = await Promise.all([
      apiFetch(`/api/projects/${projectId}/workflow-states/${a.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ order: b.order }),
      }),
      apiFetch(`/api/projects/${projectId}/workflow-states/${b.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ order: a.order }),
      }),
    ]);
    if (!resA.ok || !resB.ok) setError("Failed to reorder.");
    await load();
  }

  async function rename(state: WorkflowState, name: string) {
    if (!name || name === state.name) return;
    const res = await apiFetch(`/api/projects/${projectId}/workflow-states/${state.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (res.ok) await load();
    else setError("Failed to rename.");
  }

  async function setDefault(state: WorkflowState) {
    const res = await apiFetch(`/api/projects/${projectId}/workflow-states/${state.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_default: true }),
    });
    if (res.ok) await load();
    else setError("Failed to set default.");
  }

  return (
    <div className="flex flex-1 flex-col items-center px-4 py-10">
      <div className="flex w-full max-w-md flex-col gap-5">
        <div>
          <a href={`/projects/${projectId}`} className="text-xs text-ink-tertiary hover:text-ink-secondary">
            ← Board
          </a>
          <h1 className="text-lg font-bold tracking-tight text-ink">Workflow settings</h1>
        </div>

        {error && (
          <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>
        )}

        <div className="flex flex-col gap-2">
          {states === null && <p className="text-sm text-ink-tertiary">Loading…</p>}
          {states?.map((state, index) => (
            <div
              key={state.id}
              className="flex items-center gap-3 rounded-lg border border-line bg-surface p-3"
            >
              <div className="flex flex-col font-mono text-xs text-ink-tertiary">
                <button
                  onClick={() => swap(index, -1)}
                  disabled={index === 0}
                  className="hover:text-accent disabled:opacity-30 disabled:hover:text-ink-tertiary"
                  aria-label="Move up"
                >
                  ↑
                </button>
                <button
                  onClick={() => swap(index, 1)}
                  disabled={index === states.length - 1}
                  className="hover:text-accent disabled:opacity-30 disabled:hover:text-ink-tertiary"
                  aria-label="Move down"
                >
                  ↓
                </button>
              </div>
              <input
                className="flex-1 rounded-md border border-line bg-surface-2 px-2.5 py-1.5 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
                defaultValue={state.name}
                onBlur={(e) => rename(state, e.target.value)}
              />
              <label className="flex items-center gap-1.5 text-xs text-ink-secondary">
                <input
                  type="radio"
                  name="default-state"
                  checked={state.isDefault}
                  onChange={() => setDefault(state)}
                  className="accent-[var(--accent)]"
                />
                default
              </label>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
