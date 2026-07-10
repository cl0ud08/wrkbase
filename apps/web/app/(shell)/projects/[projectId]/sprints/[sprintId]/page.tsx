"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import {
  DndContext,
  PointerSensor,
  closestCenter,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { CSS } from "@dnd-kit/utilities";

import { apiFetch } from "../../../../../../lib/api";
import { useAuth } from "../../../../../../lib/auth-context";
import {
  mapSprint,
  mapTicket,
  mapTicketPage,
  type Sprint,
  type Ticket,
  type TicketType,
} from "../../../../../../lib/types";

const TYPE_STYLE: Record<TicketType, { label: string; text: string; bg: string }> = {
  epic: { label: "Epic", text: "text-type-epic", bg: "bg-type-epic-bg" },
  story: { label: "Story", text: "text-type-story", bg: "bg-type-story-bg" },
  task: { label: "Task", text: "text-type-task", bg: "bg-type-task-bg" },
  subtask: { label: "Subtask", text: "text-type-subtask", bg: "bg-type-subtask-bg" },
};

const STATUS_CHIP: Record<string, string> = {
  planned: "bg-neutral-bg text-neutral",
  active: "bg-info-bg text-info",
  completed: "bg-success-bg text-success",
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

function Card({ ticket, ticketKey, draggable }: { ticket: Ticket; ticketKey: string; draggable: boolean }) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: ticket.id,
    disabled: !draggable,
  });
  const style = { transform: CSS.Translate.toString(transform) };

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      className={`rounded-md border bg-surface-2 p-2.5 text-sm shadow-[var(--shadow-card)] transition-[box-shadow,border-color,opacity] duration-150 ease-out ${
        draggable ? "cursor-grab active:cursor-grabbing" : ""
      } ${
        isDragging
          ? "z-10 border-accent opacity-50 shadow-[0_0_0_3px_var(--accent-subtle)]"
          : "border-line-subtle hover:border-line-strong"
      }`}
    >
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="font-mono text-[11px] text-ink-tertiary">{ticketKey}</span>
        <TypeBadge type={ticket.type} />
      </div>
      <p className="mb-1.5 leading-snug text-ink">{ticket.title}</p>
      <span className="font-mono text-[11px] text-ink-tertiary">
        {ticket.storyPoints === null ? "unestimated" : `${ticket.storyPoints} pts`}
      </span>
    </div>
  );
}

function RetroSection({
  title,
  items,
  emptyLabel,
}: {
  title: string;
  items: string[] | null;
  emptyLabel: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <h3 className="text-xs font-semibold tracking-wide text-ink-tertiary uppercase">{title}</h3>
      {items && items.length > 0 ? (
        <ul className="flex flex-col gap-1 text-sm text-ink-secondary">
          {items.map((item, i) => (
            <li key={i} className="flex gap-1.5">
              <span aria-hidden="true">·</span>
              <span>{item}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-ink-tertiary">{emptyLabel}</p>
      )}
    </div>
  );
}

// Accent, not a semantic danger/warning color — same "AI touched this"
// vocabulary as TriageIndicator/AppSecIndicator on the board page (see
// apps/web/app/(shell)/projects/[projectId]/page.tsx), reused here
// rather than forked. Only rendered for a completed sprint: retroStatus
// is NULL for every planned/active sprint (see apps/api/app/db/models.py),
// a real, permanent state, not a "loading" one.
function SprintRetroPanel({
  sprint,
  onRegenerate,
  regenerating,
}: {
  sprint: Sprint;
  onRegenerate: () => void;
  regenerating: boolean;
}) {
  if (sprint.status !== "completed" || !sprint.retroStatus) return null;

  if (sprint.retroStatus === "pending") {
    return (
      <div className="flex items-center gap-1.5 rounded-md border border-line-subtle bg-surface-2 px-3 py-2.5 text-sm text-accent">
        <span className="h-1.5 w-1.5 flex-shrink-0 animate-pulse rounded-full bg-accent" aria-hidden="true" />
        Generating sprint retro…
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 rounded-md border border-line-subtle bg-surface-2 p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="rounded-sm bg-accent-subtle px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wide text-accent uppercase">
          AI sprint retro
        </span>
        <button
          onClick={onRegenerate}
          disabled={regenerating}
          className="text-xs text-ink-tertiary underline transition-colors duration-100 hover:text-ink-secondary disabled:opacity-50"
        >
          Regenerate
        </button>
      </div>

      {sprint.retroStatus === "failed" ? (
        <p className="text-sm text-ink-tertiary" title={sprint.retroError ?? undefined}>
          AI summary unavailable — the retro couldn&apos;t be generated this time. Try regenerating.
        </p>
      ) : (
        <>
          <p className="text-sm text-ink">{sprint.retroNarrative}</p>
          <p className="font-mono text-xs text-ink-tertiary">
            {sprint.totalPoints} / {sprint.pointsPlanned ?? sprint.totalPoints} pts completed
          </p>
          <div className="grid gap-3 sm:grid-cols-3">
            <RetroSection
              title="What got done"
              items={sprint.retroCompletedHighlights}
              emptyLabel="Nothing completed this sprint."
            />
            <RetroSection
              title="What didn't"
              items={sprint.retroIncompleteNotes}
              emptyLabel="Everything planned was finished."
            />
            <RetroSection title="Risks & blockers" items={sprint.retroRisks} emptyLabel="Nothing flagged." />
          </div>
        </>
      )}
    </div>
  );
}

function DropZone({
  id,
  title,
  subtitle,
  tickets,
  ticketPrefix,
  draggableCards,
  emptyLabel,
}: {
  id: string;
  title: string;
  subtitle: string;
  tickets: Ticket[];
  ticketPrefix: string;
  draggableCards: boolean;
  emptyLabel: string;
}) {
  const { setNodeRef, isOver } = useDroppable({ id });

  return (
    <div className="flex min-w-0 flex-1 flex-col gap-2.5">
      <div>
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        <p className="text-xs text-ink-tertiary">{subtitle}</p>
      </div>
      <div
        ref={setNodeRef}
        className={`flex min-h-[240px] flex-1 flex-col gap-2 rounded-lg border p-2.5 transition-colors duration-150 ${
          isOver ? "border-accent border-dashed bg-accent-subtle" : "border-line-subtle bg-base"
        }`}
      >
        {tickets.length === 0 && (
          <p className="flex flex-1 items-center justify-center text-center text-sm text-ink-tertiary">
            {emptyLabel}
          </p>
        )}
        {tickets.map((ticket) => (
          <Card
            key={ticket.id}
            ticket={ticket}
            ticketKey={`${ticketPrefix}-${ticket.ticketNumber}`}
            draggable={draggableCards}
          />
        ))}
      </div>
    </div>
  );
}

export default function SprintPlanningPage() {
  const { projectId, sprintId } = useParams<{ projectId: string; sprintId: string }>();
  const { user } = useAuth();

  const [sprint, setSprint] = useState<Sprint | null>(null);
  const [sprintTickets, setSprintTickets] = useState<Ticket[]>([]);
  const [backlogTickets, setBacklogTickets] = useState<Ticket[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [regenerating, setRegenerating] = useState(false);

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }));

  const load = useCallback(async () => {
    const [sprintRes, backlogRes] = await Promise.all([
      apiFetch(`/api/projects/${projectId}/sprints/${sprintId}`),
      apiFetch(`/api/projects/${projectId}/tickets/backlog?limit=100`),
    ]);
    if (sprintRes.ok) setSprint(mapSprint(await sprintRes.json()));
    if (backlogRes.ok) setBacklogTickets(mapTicketPage(await backlogRes.json()).items);
  }, [projectId, sprintId]);

  // The plain ticket-list endpoint isn't sprint-filtered — this sprint's
  // own tickets are filtered client-side against sprintId once fetched,
  // same as the backlog endpoint would be redundant here since it's
  // deliberately the *opposite* filter (sprint_id IS NULL).
  const loadSprintTickets = useCallback(async () => {
    const res = await apiFetch(`/api/projects/${projectId}/tickets`);
    if (res.ok) {
      const all: Ticket[] = (await res.json()).map(mapTicket);
      setSprintTickets(all.filter((t) => t.sprintId === sprintId));
    }
  }, [projectId, sprintId]);

  useEffect(() => {
    (async () => {
      await Promise.all([load(), loadSprintTickets()]);
    })();
  }, [load, loadSprintTickets]);

  // Simple polling, not a WebSocket subscription -- this page has no live
  // room of its own (unlike the board page's ticket-update channel), and
  // ending/regenerating a sprint is a rare, single-object, admin-only
  // action, not worth standing up new pub/sub infrastructure for. Stops
  // itself the moment retroStatus leaves "pending" -- re-fetching sprint
  // state is what actually clears the pending state, which is what makes
  // this effect re-run and see there's nothing left to poll for.
  useEffect(() => {
    if (sprint?.retroStatus !== "pending") return;
    const interval = setInterval(() => {
      void load();
    }, 2000);
    return () => clearInterval(interval);
  }, [sprint?.retroStatus, load]);

  async function moveTicket(ticketId: string, targetSprintId: string | null) {
    setError(null);
    const res = await apiFetch(`/api/projects/${projectId}/tickets/${ticketId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sprint_id: targetSprintId }),
    });
    if (res.ok) {
      await Promise.all([load(), loadSprintTickets()]);
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to move ticket.");
    }
  }

  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over) return;
    const ticketId = active.id as string;
    const inSprint = sprintTickets.some((t) => t.id === ticketId);

    if (over.id === "sprint-zone" && !inSprint) {
      void moveTicket(ticketId, sprintId);
    } else if (over.id === "backlog-zone" && inSprint) {
      void moveTicket(ticketId, null);
    }
  }

  async function handleStart() {
    setError(null);
    setBusy(true);
    const res = await apiFetch(`/api/projects/${projectId}/sprints/${sprintId}/start`, { method: "POST" });
    if (res.ok) {
      await load();
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to start sprint.");
    }
    setBusy(false);
  }

  async function handleComplete() {
    if (
      !confirm(
        "Complete this sprint? Any ticket not in the project's final column will be returned to the backlog.",
      )
    ) {
      return;
    }
    setError(null);
    setBusy(true);
    const res = await apiFetch(`/api/projects/${projectId}/sprints/${sprintId}/complete`, { method: "POST" });
    if (res.ok) {
      await Promise.all([load(), loadSprintTickets()]);
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to complete sprint.");
    }
    setBusy(false);
  }

  async function handleRegenerate() {
    setError(null);
    setRegenerating(true);
    const res = await apiFetch(`/api/projects/${projectId}/sprints/${sprintId}/retro/regenerate`, {
      method: "POST",
    });
    if (res.ok) {
      await load();
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to regenerate sprint retro.");
    }
    setRegenerating(false);
  }

  if (!sprint) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-sm text-ink-tertiary">Loading…</p>
      </div>
    );
  }

  const canDrag = sprint.status !== "completed";

  return (
    <div className="flex flex-1 flex-col gap-4 px-4 py-5">
      <div className="flex items-center justify-between gap-3">
        <div>
          <a href={`/projects/${projectId}/sprints`} className="text-xs text-ink-tertiary hover:text-ink-secondary">
            ← Sprints
          </a>
          <div className="flex items-center gap-2">
            <h1 className="text-lg font-bold tracking-tight text-ink">{sprint.name}</h1>
            <span
              className={`flex items-center gap-1.5 rounded-sm px-2 py-0.5 text-xs font-medium ${STATUS_CHIP[sprint.status]}`}
            >
              {sprint.status}
            </span>
          </div>
          <p className="text-xs text-ink-tertiary">
            {sprint.startDate} → {sprint.endDate} · {sprint.totalPoints} pts committed
          </p>
        </div>
        <div className="flex gap-2">
          {sprint.status === "planned" && (
            <button
              onClick={handleStart}
              disabled={busy}
              className="rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
            >
              Start sprint
            </button>
          )}
          {sprint.status === "active" && (
            <button
              onClick={handleComplete}
              disabled={busy}
              className="rounded-md border border-line px-3.5 py-1.5 text-sm font-semibold text-ink transition-colors duration-100 hover:border-line-strong hover:bg-hover disabled:opacity-50"
            >
              Complete sprint
            </button>
          )}
        </div>
      </div>

      {error && <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>}

      {sprint.goal && <p className="text-sm text-ink-secondary">{sprint.goal}</p>}

      <SprintRetroPanel sprint={sprint} onRegenerate={handleRegenerate} regenerating={regenerating} />

      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
        <div className="flex flex-1 flex-col gap-4 lg:flex-row">
          <DropZone
            id="backlog-zone"
            title="Backlog"
            subtitle="Drag into the sprint to plan it in"
            tickets={backlogTickets}
            ticketPrefix={user?.ticketPrefix ?? ""}
            draggableCards={canDrag}
            emptyLabel="Backlog is empty"
          />
          <DropZone
            id="sprint-zone"
            title={`${sprint.name} (${sprintTickets.length})`}
            subtitle={canDrag ? "Drag out to return to the backlog" : "Sprint is completed — read only"}
            tickets={sprintTickets}
            ticketPrefix={user?.ticketPrefix ?? ""}
            draggableCards={canDrag}
            emptyLabel="Nothing planned yet"
          />
        </div>
      </DndContext>
    </div>
  );
}
