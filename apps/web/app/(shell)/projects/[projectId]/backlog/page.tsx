"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { apiFetch } from "../../../../../lib/api";
import { useAuth } from "../../../../../lib/auth-context";
import { mapSprint, mapTicketPage, type Sprint, type Ticket, type TicketType } from "../../../../../lib/types";

const PAGE_SIZE = 50;

const TYPE_STYLE: Record<TicketType, { label: string; text: string; bg: string }> = {
  epic: { label: "Epic", text: "text-type-epic", bg: "bg-type-epic-bg" },
  story: { label: "Story", text: "text-type-story", bg: "bg-type-story-bg" },
  task: { label: "Task", text: "text-type-task", bg: "bg-type-task-bg" },
  subtask: { label: "Subtask", text: "text-type-subtask", bg: "bg-type-subtask-bg" },
};

function TypeBadge({ type }: { type: TicketType }) {
  const style = TYPE_STYLE[type];
  return (
    <span
      className={`rounded-sm px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wide uppercase ${style.text} ${style.bg}`}
    >
      {style.label}
    </span>
  );
}

export default function BacklogPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const { user } = useAuth();

  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [sprints, setSprints] = useState<Sprint[]>([]);
  const [targetSprintId, setTargetSprintId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [assigning, setAssigning] = useState(false);

  const loadPage = useCallback(
    async (nextOffset: number) => {
      setLoading(true);
      const res = await apiFetch(
        `/api/projects/${projectId}/tickets/backlog?limit=${PAGE_SIZE}&offset=${nextOffset}`,
      );
      if (res.ok) {
        const page = mapTicketPage(await res.json());
        setTickets((prev) => (nextOffset === 0 ? page.items : [...prev, ...page.items]));
        setTotal(page.total);
        setOffset(nextOffset);
      }
      setLoading(false);
    },
    [projectId],
  );

  const loadSprints = useCallback(async () => {
    const res = await apiFetch(`/api/projects/${projectId}/sprints`);
    if (res.ok) {
      const all: Sprint[] = (await res.json()).map(mapSprint);
      // A completed sprint can't accept new tickets (see POST .../assign) —
      // don't even offer it as a target.
      const eligible = all.filter((s) => s.status !== "completed");
      setSprints(eligible);
      if (eligible.length > 0) setTargetSprintId((cur) => cur || eligible[0].id);
    }
  }, [projectId]);

  useEffect(() => {
    (async () => {
      await Promise.all([loadPage(0), loadSprints()]);
    })();
  }, [loadPage, loadSprints]);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelected((prev) => (prev.size === tickets.length ? new Set() : new Set(tickets.map((t) => t.id))));
  }

  async function handleAssign() {
    if (selected.size === 0 || !targetSprintId) return;
    setError(null);
    setAssigning(true);

    const res = await apiFetch(`/api/projects/${projectId}/sprints/${targetSprintId}/assign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket_ids: Array.from(selected) }),
    });

    if (res.ok) {
      setSelected(new Set());
      await loadPage(0);
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to add tickets to the sprint.");
    }
    setAssigning(false);
  }

  const hasMore = total !== null && tickets.length < total;

  return (
    <div className="flex flex-1 flex-col items-center px-4 py-10">
      <div className="flex w-full max-w-3xl flex-col gap-5">
        <div>
          <a href={`/projects/${projectId}`} className="text-xs text-ink-tertiary hover:text-ink-secondary">
            ← Board
          </a>
          <h1 className="text-lg font-bold tracking-tight text-ink">Backlog</h1>
        </div>

        {error && <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>}

        {sprints.length > 0 && (
          <div className="flex flex-wrap items-center gap-2.5 rounded-lg border border-line bg-surface p-3">
            <span className="text-sm text-ink-secondary">
              {selected.size > 0 ? `${selected.size} selected` : "Select tickets to plan"}
            </span>
            <select
              className="rounded-md border border-line bg-surface-2 px-2.5 py-1.5 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
              value={targetSprintId}
              onChange={(e) => setTargetSprintId(e.target.value)}
            >
              {sprints.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.status})
                </option>
              ))}
            </select>
            <button
              onClick={handleAssign}
              disabled={selected.size === 0 || assigning}
              className="rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
            >
              {assigning ? "Adding…" : "Add to sprint"}
            </button>
          </div>
        )}

        {loading && tickets.length === 0 && <p className="text-sm text-ink-tertiary">Loading…</p>}

        {!loading && tickets.length === 0 && (
          <div className="flex flex-col items-start gap-2.5 rounded-lg border border-dashed border-line px-5 py-9">
            <h2 className="text-sm font-semibold text-ink">Backlog is empty</h2>
            <p className="max-w-xs text-sm text-ink-secondary">
              Every ticket is either assigned to a sprint or hasn&apos;t been created yet.
            </p>
          </div>
        )}

        {tickets.length > 0 && (
          <div className="flex flex-col overflow-hidden rounded-lg border border-line">
            <div className="flex items-center gap-3 border-b border-line-subtle bg-surface-2 px-3 py-2">
              <input
                type="checkbox"
                checked={selected.size === tickets.length && tickets.length > 0}
                onChange={toggleAll}
                aria-label="Select all"
              />
              <span className="font-mono text-[11px] tracking-wider text-ink-tertiary uppercase">
                {total ?? tickets.length} in backlog
              </span>
            </div>
            {tickets.map((ticket, i) => (
              <label
                key={ticket.id}
                className={`flex cursor-pointer items-center gap-3 bg-surface px-3 py-2.5 transition-colors duration-100 hover:bg-hover ${
                  i !== 0 ? "border-t border-line-subtle" : ""
                }`}
              >
                <input
                  type="checkbox"
                  checked={selected.has(ticket.id)}
                  onChange={() => toggle(ticket.id)}
                  onClick={(e) => e.stopPropagation()}
                />
                <span className="font-mono text-[11px] text-ink-tertiary">
                  {user?.ticketPrefix}-{ticket.ticketNumber}
                </span>
                <TypeBadge type={ticket.type} />
                <span className="min-w-0 flex-1 truncate text-sm text-ink">{ticket.title}</span>
                <span className="flex-shrink-0 font-mono text-xs text-ink-tertiary">
                  {ticket.storyPoints === null ? "—" : `${ticket.storyPoints} pts`}
                </span>
              </label>
            ))}
          </div>
        )}

        {hasMore && (
          <button
            onClick={() => loadPage(offset + PAGE_SIZE)}
            disabled={loading}
            className="self-center rounded-md border border-line px-4 py-2 text-sm font-medium text-ink transition-colors duration-100 hover:border-line-strong hover:bg-hover disabled:opacity-50"
          >
            {loading ? "Loading…" : "Load more"}
          </button>
        )}
      </div>
    </div>
  );
}
