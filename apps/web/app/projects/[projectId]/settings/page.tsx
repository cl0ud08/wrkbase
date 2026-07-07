"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { apiFetch } from "../../../../lib/api";
import { mapWorkflowState, type WorkflowState } from "../../../../lib/types";

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
    <div className="flex flex-1 flex-col items-center gap-6 bg-zinc-50 px-6 py-16 font-sans dark:bg-black">
      <div className="w-full max-w-md">
        <a
          href={`/projects/${projectId}`}
          className="text-sm text-zinc-500 hover:underline dark:text-zinc-500"
        >
          &larr; Board
        </a>
        <h1 className="text-2xl font-semibold tracking-tight text-black dark:text-zinc-50">
          Workflow settings
        </h1>
      </div>

      {error && <p className="text-sm text-red-500">{error}</p>}

      <div className="flex w-full max-w-md flex-col gap-2">
        {states === null && (
          <p className="text-sm text-zinc-600 dark:text-zinc-400">Loading...</p>
        )}
        {states?.map((state, index) => (
          <div
            key={state.id}
            className="flex items-center gap-3 rounded-xl border border-black/[.08] p-3 dark:border-white/[.145]"
          >
            <div className="flex flex-col text-xs">
              <button
                onClick={() => swap(index, -1)}
                disabled={index === 0}
                className="disabled:opacity-30"
                aria-label="Move up"
              >
                &uarr;
              </button>
              <button
                onClick={() => swap(index, 1)}
                disabled={index === states.length - 1}
                className="disabled:opacity-30"
                aria-label="Move down"
              >
                &darr;
              </button>
            </div>
            <input
              className="flex-1 rounded-md border border-black/[.08] px-2 py-1 text-sm dark:border-white/[.145] dark:bg-zinc-900"
              defaultValue={state.name}
              onBlur={(e) => rename(state, e.target.value)}
            />
            <label className="flex items-center gap-1 text-xs text-zinc-500 dark:text-zinc-500">
              <input
                type="radio"
                name="default-state"
                checked={state.isDefault}
                onChange={() => setDefault(state)}
              />
              default
            </label>
          </div>
        ))}
      </div>
    </div>
  );
}
