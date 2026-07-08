"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import {
  DndContext,
  PointerSensor,
  closestCorners,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { SortableContext, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

import { apiFetch } from "../../../../lib/api";
import { useAuth } from "../../../../lib/auth-context";
import {
  mapMember,
  mapTicket,
  mapWorkflowState,
  type Member,
  type Ticket,
  type TicketType,
  type WorkflowState,
} from "../../../../lib/types";

// First two chars of the email's local part — no display-name field exists
// on User yet, so email is the only identity data available to derive a
// compact badge from.
function initials(email: string): string {
  return email.slice(0, 2).toUpperCase();
}

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

function columnTickets(tickets: Ticket[], stateId: string): Ticket[] {
  return tickets.filter((t) => t.workflowStateId === stateId).sort((a, b) => a.position - b.position);
}

// Gap-based position for wherever a card lands within a column: halve the
// gap to the neighbor above when dropped at the top, add a full gap past
// the last card at the bottom, or split the gap between two neighbors in
// the middle. Keeps every drop an O(1) write — never renumbers the rest
// of the column the way a plain sequential integer position would.
function computePosition(siblings: Ticket[], index: number): number {
  if (siblings.length === 0) return 1024;
  if (index <= 0) return siblings[0].position / 2;
  if (index >= siblings.length) return siblings[siblings.length - 1].position + 1024;
  return (siblings[index - 1].position + siblings[index].position) / 2;
}

function Card({
  ticket,
  ticketKey,
  members,
  onAssigneeChange,
}: {
  ticket: Ticket;
  ticketKey: string;
  members: Member[];
  onAssigneeChange: (ticketId: string, assigneeId: string | null) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: ticket.id,
  });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };
  const assignee = members.find((m) => m.id === ticket.assigneeId);

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      className={`group cursor-grab rounded-md border bg-surface-2 p-2.5 text-sm shadow-[var(--shadow-card)] transition-[transform,box-shadow,border-color,opacity] duration-150 ease-out active:cursor-grabbing ${
        isDragging
          ? "border-accent opacity-50 shadow-[0_0_0_3px_var(--accent-subtle)]"
          : "border-line-subtle hover:-translate-y-0.5 hover:border-line-strong hover:shadow-[var(--shadow-card-hover)]"
      }`}
    >
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="font-mono text-[11px] text-ink-tertiary">{ticketKey}</span>
        <TypeBadge type={ticket.type} />
      </div>
      <p className="mb-2 leading-snug text-ink">{ticket.title}</p>
      <div className="flex items-center justify-between gap-2">
        <span
          title={assignee ? assignee.email : "Unassigned"}
          className={`flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-[4px] font-mono text-[9px] font-semibold ${
            assignee
              ? "border border-line bg-hover text-ink-secondary"
              : "border border-dashed border-line text-ink-tertiary"
          }`}
        >
          {assignee ? initials(assignee.email) : "?"}
        </span>
        <select
          value={ticket.assigneeId ?? ""}
          onChange={(e) => onAssigneeChange(ticket.id, e.target.value || null)}
          // dnd-kit's drag listeners are spread on this card's outer div
          // ({...listeners} above) and fire on pointerdown; without
          // stopping propagation here, opening this native <select> would
          // also be read as the start of a drag gesture on the card
          // underneath it. Any future interactive control added directly
          // onto a card needs the same guard.
          onPointerDown={(e) => e.stopPropagation()}
          className="min-w-0 flex-1 cursor-pointer truncate rounded-sm border-0 bg-transparent py-0.5 text-right text-xs text-ink-tertiary hover:text-ink-secondary"
        >
          <option value="">Unassigned</option>
          {members.map((member) => (
            <option key={member.id} value={member.id}>
              {member.email}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

function Column({
  state,
  tickets,
  ticketPrefix,
  members,
  onAssigneeChange,
}: {
  state: WorkflowState;
  tickets: Ticket[];
  ticketPrefix: string;
  members: Member[];
  onAssigneeChange: (ticketId: string, assigneeId: string | null) => void;
}) {
  // useDroppable on the column itself, in addition to the SortableContext
  // around its cards: dnd-kit's sortable list only gives you a drop target
  // for each *item*, so an empty column (zero sortable children) has
  // nothing to drop onto without this — dragging a card into an empty
  // column silently does nothing otherwise. This is the one dnd-kit
  // gotcha that actually bit during this build; both are needed together.
  const { setNodeRef, isOver } = useDroppable({ id: state.id });

  return (
    <div
      ref={setNodeRef}
      className={`flex w-[268px] flex-shrink-0 flex-col gap-2 rounded-lg border p-2 transition-colors duration-150 ${
        isOver
          ? "border-accent border-dashed bg-accent-subtle"
          : "border-line-subtle bg-surface"
      }`}
    >
      <div className="flex items-center justify-between px-1 pt-0.5">
        <h3 className="text-[11px] font-bold tracking-wide text-ink-secondary uppercase">
          {state.name}
        </h3>
        <span className="rounded-full bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-ink-tertiary">
          {tickets.length}
        </span>
      </div>
      <SortableContext items={tickets.map((t) => t.id)} strategy={verticalListSortingStrategy}>
        <div className="flex min-h-[48px] flex-col gap-1.5">
          {tickets.map((ticket) => (
            <Card
              key={ticket.id}
              ticket={ticket}
              ticketKey={`${ticketPrefix}-${ticket.ticketNumber}`}
              members={members}
              onAssigneeChange={onAssigneeChange}
            />
          ))}
          {tickets.length === 0 && (
            <div className="flex min-h-[48px] items-center justify-center rounded-md border border-dashed border-line-subtle">
              <span className="text-xs text-ink-tertiary">Drop here</span>
            </div>
          )}
        </div>
      </SortableContext>
    </div>
  );
}

export default function ProjectBoardPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const { user } = useAuth();

  const [states, setStates] = useState<WorkflowState[] | null>(null);
  const [tickets, setTickets] = useState<Ticket[] | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }));

  const load = useCallback(async () => {
    const [statesRes, ticketsRes, membersRes] = await Promise.all([
      apiFetch(`/api/projects/${projectId}/workflow-states`),
      apiFetch(`/api/projects/${projectId}/tickets`),
      apiFetch(`/api/org/members`),
    ]);
    if (statesRes.ok) {
      const data = await statesRes.json();
      setStates(data.map(mapWorkflowState).sort((a: WorkflowState, b: WorkflowState) => a.order - b.order));
    }
    if (ticketsRes.ok) {
      setTickets((await ticketsRes.json()).map(mapTicket));
    }
    if (membersRes.ok) {
      setMembers((await membersRes.json()).map(mapMember));
    }
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

    const res = await apiFetch(`/api/projects/${projectId}/tickets`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "task", title, description: null }),
    });

    if (res.ok) {
      setTitle("");
      setShowForm(false);
      await load();
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to create ticket.");
    }
    setSubmitting(false);
  }

  // Optimistic: this was a plain "drag -> PATCH -> reload from the
  // server" implementation first (no client-side move before the API
  // response), confirmed working end to end — signup, create, drag
  // between columns, cross-org 404s, all verified — before adding the
  // optimism below. Moving the card locally right away makes the drag
  // feel instant instead of snapping back until the round trip finishes;
  // the pre-move snapshot is kept specifically so a failed request can
  // restore it exactly, rather than falling back to a full reload.
  async function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || !tickets || !states) return;

    const activeTicket = tickets.find((t) => t.id === active.id);
    if (!activeTicket) return;

    const overIsColumn = states.some((s) => s.id === over.id);
    let targetStateId: string;
    let targetIndex: number;

    if (overIsColumn) {
      targetStateId = String(over.id);
      targetIndex = columnTickets(tickets, targetStateId).length;
    } else {
      const overTicket = tickets.find((t) => t.id === over.id);
      if (!overTicket) return;
      targetStateId = overTicket.workflowStateId;
      targetIndex = columnTickets(tickets, targetStateId).findIndex((t) => t.id === overTicket.id);
    }

    if (targetStateId === activeTicket.workflowStateId && targetIndex === -1) return;

    const siblings = columnTickets(tickets, targetStateId).filter((t) => t.id !== activeTicket.id);
    const position = computePosition(siblings, targetIndex);

    const previousTickets = tickets;
    setTickets(
      tickets.map((t) =>
        t.id === activeTicket.id ? { ...t, workflowStateId: targetStateId, position } : t,
      ),
    );

    const res = await apiFetch(`/api/projects/${projectId}/tickets/${activeTicket.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow_state_id: targetStateId, position }),
    });
    if (!res.ok) {
      setError("Failed to move ticket.");
      setTickets(previousTickets);
    }
  }

  // Same optimistic-then-rollback shape as handleDragEnd: assigning is
  // just as core a collaborative action as moving a card (see
  // _COLLABORATIVE_FIELDS in the backend), so it gets the same instant-
  // feedback treatment rather than waiting on a round trip.
  async function handleAssigneeChange(ticketId: string, assigneeId: string | null) {
    if (!tickets) return;
    const previousTickets = tickets;
    setTickets(tickets.map((t) => (t.id === ticketId ? { ...t, assigneeId } : t)));

    const res = await apiFetch(`/api/projects/${projectId}/tickets/${ticketId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ assignee_id: assigneeId }),
    });
    if (!res.ok) {
      setError("Failed to change assignee.");
      setTickets(previousTickets);
    }
  }

  const isEmpty = tickets !== null && tickets.length === 0;

  return (
    <div className="flex flex-1 flex-col gap-4 px-4 py-5">
      <div className="flex items-center justify-between gap-3">
        <div>
          <a href="/dashboard" className="text-xs text-ink-tertiary hover:text-ink-secondary">
            ← Projects
          </a>
          <h1 className="text-lg font-bold tracking-tight text-ink">Board</h1>
        </div>
        <div className="flex gap-2">
          <a
            href={`/projects/${projectId}/settings`}
            className="rounded-md border border-line px-3 py-1.5 text-sm font-medium text-ink transition-colors duration-100 hover:border-line-strong hover:bg-hover"
          >
            Workflow settings
          </a>
          <button
            onClick={() => setShowForm((v) => !v)}
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
          >
            {showForm ? "Cancel" : "New ticket"}
          </button>
        </div>
      </div>

      {showForm && (
        <form
          onSubmit={handleCreate}
          className="flex w-full max-w-md flex-col gap-2.5 rounded-lg border border-line bg-surface p-3"
        >
          <input
            className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
            placeholder="Ticket title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
          />
          {error && (
            <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>
          )}
          <button
            type="submit"
            disabled={submitting}
            className="self-start rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
          >
            {submitting ? "Creating…" : "Create"}
          </button>
        </form>
      )}

      {error && !showForm && (
        <p className="w-fit rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>
      )}

      {states === null || tickets === null ? (
        <p className="text-sm text-ink-tertiary">Loading…</p>
      ) : isEmpty ? (
        <div className="flex flex-col items-start gap-2.5 rounded-lg border border-dashed border-line px-5 py-9">
          <div className="flex h-8 w-8 items-center justify-center rounded-md border border-line bg-surface-2 font-mono text-sm text-accent">
            +
          </div>
          <h2 className="text-sm font-semibold text-ink">No tickets yet</h2>
          <p className="max-w-xs text-sm text-ink-secondary">
            Create one to get started — it&apos;ll land in{" "}
            {states.find((s) => s.isDefault)?.name ?? "the first column"}.
          </p>
          <button
            onClick={() => setShowForm(true)}
            className="mt-1 rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
          >
            New ticket
          </button>
        </div>
      ) : (
        <DndContext sensors={sensors} collisionDetection={closestCorners} onDragEnd={handleDragEnd}>
          <div className="flex gap-3 overflow-x-auto pb-3">
            {states.map((state) => (
              <Column
                key={state.id}
                state={state}
                tickets={columnTickets(tickets, state.id)}
                ticketPrefix={user?.ticketPrefix ?? "TKT"}
                members={members}
                onAssigneeChange={handleAssigneeChange}
              />
            ))}
          </div>
        </DndContext>
      )}
    </div>
  );
}
