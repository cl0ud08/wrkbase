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

import { apiFetch } from "../../../lib/api";
import { mapTicket, mapWorkflowState, type Ticket, type WorkflowState } from "../../../lib/types";

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

function Card({ ticket }: { ticket: Ticket }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: ticket.id,
  });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  };
  return (
    <div
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      className="cursor-grab rounded-lg border border-black/[.08] bg-white p-3 text-sm shadow-sm active:cursor-grabbing dark:border-white/[.145] dark:bg-zinc-900"
    >
      <span className="text-xs uppercase tracking-wide text-zinc-500 dark:text-zinc-500">
        {ticket.type}
      </span>
      <p className="font-medium text-black dark:text-zinc-50">{ticket.title}</p>
    </div>
  );
}

function Column({
  state,
  tickets,
}: {
  state: WorkflowState;
  tickets: Ticket[];
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
      className={`flex w-72 flex-shrink-0 flex-col gap-2 rounded-xl border p-3 transition-colors ${
        isOver ? "bg-black/[.03] dark:bg-white/[.05]" : ""
      } border-black/[.08] dark:border-white/[.145]`}
    >
      <h3 className="px-1 text-sm font-semibold text-zinc-700 dark:text-zinc-300">{state.name}</h3>
      <SortableContext items={tickets.map((t) => t.id)} strategy={verticalListSortingStrategy}>
        <div className="flex min-h-[40px] flex-col gap-2">
          {tickets.map((ticket) => (
            <Card key={ticket.id} ticket={ticket} />
          ))}
        </div>
      </SortableContext>
    </div>
  );
}

export default function ProjectBoardPage() {
  const { projectId } = useParams<{ projectId: string }>();

  const [states, setStates] = useState<WorkflowState[] | null>(null);
  const [tickets, setTickets] = useState<Ticket[] | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }));

  const load = useCallback(async () => {
    const [statesRes, ticketsRes] = await Promise.all([
      apiFetch(`/api/projects/${projectId}/workflow-states`),
      apiFetch(`/api/projects/${projectId}/tickets`),
    ]);
    if (statesRes.ok) {
      const data = await statesRes.json();
      setStates(data.map(mapWorkflowState).sort((a: WorkflowState, b: WorkflowState) => a.order - b.order));
    }
    if (ticketsRes.ok) {
      setTickets((await ticketsRes.json()).map(mapTicket));
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

  return (
    <div className="flex flex-1 flex-col gap-6 bg-zinc-50 px-6 py-10 font-sans dark:bg-black">
      <div className="flex items-center justify-between">
        <div>
          <a href="/dashboard" className="text-sm text-zinc-500 hover:underline dark:text-zinc-500">
            &larr; Projects
          </a>
          <h1 className="text-2xl font-semibold tracking-tight text-black dark:text-zinc-50">
            Board
          </h1>
        </div>
        <div className="flex gap-3">
          <a
            href={`/projects/${projectId}/settings`}
            className="rounded-full border border-black/[.08] px-5 py-2 text-sm font-medium transition-colors hover:bg-black/[.04] dark:border-white/[.145] dark:hover:bg-[#1a1a1a]"
          >
            Workflow settings
          </a>
          <button
            onClick={() => setShowForm((v) => !v)}
            className="rounded-full bg-foreground px-5 py-2 text-sm font-medium text-background transition-colors hover:bg-[#383838] dark:hover:bg-[#ccc]"
          >
            {showForm ? "Cancel" : "New ticket"}
          </button>
        </div>
      </div>

      {showForm && (
        <form
          onSubmit={handleCreate}
          className="flex w-full max-w-md flex-col gap-3 rounded-xl border border-black/[.08] p-4 dark:border-white/[.145]"
        >
          <input
            className="rounded-md border border-black/[.08] px-3 py-2 text-sm dark:border-white/[.145] dark:bg-zinc-900"
            placeholder="Ticket title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
          />
          {error && <p className="text-sm text-red-500">{error}</p>}
          <button
            type="submit"
            disabled={submitting}
            className="self-start rounded-full bg-foreground px-4 py-1.5 text-sm font-medium text-background transition-colors hover:bg-[#383838] disabled:opacity-50 dark:hover:bg-[#ccc]"
          >
            {submitting ? "Creating..." : "Create"}
          </button>
        </form>
      )}

      {states === null || tickets === null ? (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">Loading...</p>
      ) : (
        <DndContext sensors={sensors} collisionDetection={closestCorners} onDragEnd={handleDragEnd}>
          <div className="flex gap-4 overflow-x-auto pb-4">
            {states.map((state) => (
              <Column key={state.id} state={state} tickets={columnTickets(tickets, state.id)} />
            ))}
          </div>
        </DndContext>
      )}
    </div>
  );
}
