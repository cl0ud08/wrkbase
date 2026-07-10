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
  mapDuplicateCandidates,
  mapMember,
  mapParsedTicketCandidate,
  mapTicket,
  mapTicketUpdatedEvent,
  mapWorkflowState,
  type DuplicateCandidate,
  type Member,
  type ParsedTicketCandidate,
  type Ticket,
  type TicketPriority,
  type TicketType,
  type WorkflowState,
} from "../../../../lib/types";
import { connectProjectSocket } from "../../../../lib/ws";

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

// Mapped onto this app's existing semantic tokens (danger/warning/info/
// neutral), not a new priority-specific palette — critical genuinely is
// this app's "danger" concept, not a fifth color needing its own meaning.
const PRIORITY_STYLE: Record<TicketPriority, { label: string; text: string; bg: string }> = {
  critical: { label: "Critical", text: "text-danger", bg: "bg-danger-bg" },
  high: { label: "High", text: "text-warning", bg: "bg-warning-bg" },
  medium: { label: "Medium", text: "text-info", bg: "bg-info-bg" },
  low: { label: "Low", text: "text-neutral", bg: "bg-neutral-bg" },
};

function PriorityBadge({ priority }: { priority: TicketPriority }) {
  const style = PRIORITY_STYLE[priority];
  return (
    <span
      className={`rounded-sm px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wide uppercase ${style.text} ${style.bg}`}
    >
      {style.label}
    </span>
  );
}

// pending_triage/triaged/failed clears or updates itself the instant the
// worker's live update arrives over the board's own WebSocket connection
// — no polling, no separate fetch, the exact same onmessage splice every
// other live board update already uses.
function TriageIndicator({ ticket }: { ticket: Ticket }) {
  if (ticket.triageStatus === "pending") {
    return (
      <div className="mb-2 flex items-center gap-1.5 text-[10px] text-ink-tertiary">
        <span className="h-1 w-1 flex-shrink-0 animate-pulse rounded-full bg-accent" aria-hidden="true" />
        AI triaging…
      </div>
    );
  }
  if (ticket.triageStatus === "failed") {
    return (
      <div
        className="mb-2 flex items-center gap-1.5 text-[10px] text-danger"
        title={ticket.triageError ?? "Triage failed"}
      >
        <span className="h-1 w-1 flex-shrink-0 rounded-full bg-danger" aria-hidden="true" />
        Triage failed
      </div>
    );
  }
  // triaged: real priority (never absent once triageStatus is "triaged"
  // — both are always set together, see worker/main.py) plus any
  // suggested labels, title-tooltipped with the model's own one-sentence
  // reasoning so a human can see *why*, not just trust the badge.
  return (
    <div
      className="mb-2 flex flex-wrap items-center gap-1"
      title={ticket.triageReasoning ?? undefined}
    >
      {ticket.priority && <PriorityBadge priority={ticket.priority} />}
      {ticket.labels?.map((label) => (
        <span
          key={label}
          className="rounded-sm bg-hover px-1.5 py-0.5 font-mono text-[10px] text-ink-tertiary"
        >
          {label}
        </span>
      ))}
    </div>
  );
}

// Types a parsed candidate can ever carry (see ticket_parse.py's own
// prompt) — deliberately excludes "subtask", which needs a specific
// parent ticket that free text alone can't determine.
const PARSE_TYPE_OPTIONS: Exclude<TicketType, "subtask">[] = ["epic", "story", "task"];

// Non-blocking by construction: a failed check (network error, the
// backend's own 503 when both embedding attempts are exhausted) just
// means no warning is shown, never something that stops a ticket from
// being created — the same "don't hard-block on an AI feature" pattern
// as everywhere else this app calls out to an LLM synchronously.
async function checkDuplicates(
  projectId: string,
  title: string,
  description: string | null,
): Promise<DuplicateCandidate[]> {
  const res = await apiFetch(`/api/projects/${projectId}/tickets/check-duplicates`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, description }),
  });
  if (!res.ok) return [];
  return mapDuplicateCandidates(await res.json());
}

// No single-ticket page exists in this app yet (only the board, backlog,
// and sprint views) -- links point at the board itself, the closest
// existing place a human can actually go find the matched ticket, rather
// than a deep link this app has nowhere to send them to yet.
function DuplicateWarning({
  projectId,
  ticketPrefix,
  matches,
}: {
  projectId: string;
  ticketPrefix: string;
  matches: DuplicateCandidate[];
}) {
  if (matches.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5 rounded-md bg-warning-bg px-3 py-2 text-sm text-warning">
      <p>Possible duplicate{matches.length > 1 ? "s" : ""} found:</p>
      <ul className="flex flex-col gap-1">
        {matches.map((m) => (
          <li key={m.ticketId} className="flex items-center gap-1.5">
            <a
              href={`/projects/${projectId}`}
              target="_blank"
              rel="noreferrer"
              className="underline hover:no-underline"
            >
              {ticketPrefix}-{m.ticketNumber}: {m.title}
            </a>
            <span className="text-[11px] text-ink-tertiary">
              ({Math.round(m.similarity * 100)}% similar)
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// Natural-language ticket creation: parse, then review/edit, then confirm
// — never auto-submits on parse, same "don't auto-create silently"
// principle as everywhere else AI touches this app. Self-contained: owns
// its own text/candidate/edit state so ProjectBoardPage only needs to
// know when it's done (onCreated) or dismissed (onCancel).
function ParseTicketPanel({
  projectId,
  ticketPrefix,
  onCreated,
  onCancel,
}: {
  projectId: string;
  ticketPrefix: string;
  onCreated: () => Promise<void>;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  const [parsing, setParsing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [candidate, setCandidate] = useState<ParsedTicketCandidate | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [editType, setEditType] = useState<Exclude<TicketType, "subtask">>("task");
  const [duplicates, setDuplicates] = useState<DuplicateCandidate[]>([]);
  const [checkingDuplicates, setCheckingDuplicates] = useState(false);

  async function runDuplicateCheck(title: string, description: string | null) {
    setCheckingDuplicates(true);
    setDuplicates(await checkDuplicates(projectId, title, description));
    setCheckingDuplicates(false);
  }

  async function handleParse(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setCandidate(null);
    setDuplicates([]);
    setParsing(true);

    const res = await apiFetch(`/api/projects/${projectId}/tickets/parse`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });

    if (res.ok) {
      const parsed = mapParsedTicketCandidate(await res.json());
      setCandidate(parsed);
      if (parsed.confident) {
        setEditTitle(parsed.title ?? "");
        setEditDescription(parsed.description ?? "");
        setEditType((parsed.type as Exclude<TicketType, "subtask">) ?? "task");
        // The user is reviewing this candidate right now, deciding
        // whether to create it -- the same "there's a waiting caller"
        // moment that makes the parse call itself synchronous applies
        // here too, so the check runs immediately rather than waiting
        // for an explicit action.
        await runDuplicateCheck(parsed.title ?? "", parsed.description);
      }
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Couldn't parse that. Try again, or fill in the ticket manually.");
    }
    setParsing(false);
  }

  async function handleConfirm(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setConfirming(true);

    // The exact same creation endpoint every other ticket goes through —
    // no "already parsed" flag. It doesn't care whether editTitle came
    // from a form or an edited AI suggestion, and it still queues the
    // usual async triage job, which is what actually determines this
    // ticket's real priority/labels, not the preview above.
    const res = await apiFetch(`/api/projects/${projectId}/tickets`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: editType, title: editTitle, description: editDescription || null }),
    });

    if (res.ok) {
      await onCreated();
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to create ticket.");
    }
    setConfirming(false);
  }

  return (
    <div className="flex w-full max-w-md flex-col gap-2.5 rounded-lg border border-line bg-surface p-3">
      {!candidate && (
        <form onSubmit={handleParse} className="flex flex-col gap-2.5">
          <textarea
            className="min-h-[64px] rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
            placeholder="Describe your ticket… e.g. “create a bug for login failing on Safari, high priority”"
            value={text}
            onChange={(e) => setText(e.target.value)}
            required
          />
          {error && <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>}
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={parsing}
              className="self-start rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
            >
              {parsing ? "Parsing…" : "Parse"}
            </button>
            <button
              type="button"
              onClick={onCancel}
              className="self-start rounded-md px-3 py-1.5 text-sm font-medium text-ink-secondary hover:text-ink"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {candidate && !candidate.confident && (
        <div className="flex flex-col gap-2.5">
          <p className="rounded-md bg-warning-bg px-3 py-2 text-sm text-warning">
            Not sure what to make of that: {candidate.clarification}
          </p>
          <div className="flex gap-2">
            <button
              onClick={() => setCandidate(null)}
              className="self-start rounded-md border border-line px-3 py-1.5 text-sm font-medium text-ink hover:bg-hover"
            >
              Try rephrasing
            </button>
            <button
              onClick={() => {
                // Honest fallback, not a fabricated guess: hand the raw
                // text over as a starting point for the title rather than
                // pretending the AI extracted one.
                setEditTitle(text);
                setEditDescription("");
                setEditType("task");
                setCandidate({ ...candidate, confident: true });
                setDuplicates([]);
              }}
              className="self-start rounded-md px-3 py-1.5 text-sm font-medium text-ink-secondary hover:text-ink"
            >
              Fill in manually
            </button>
          </div>
        </div>
      )}

      {candidate && candidate.confident && (
        <form onSubmit={handleConfirm} className="flex flex-col gap-2.5">
          <input
            className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
            placeholder="Ticket title"
            value={editTitle}
            onChange={(e) => setEditTitle(e.target.value)}
            required
          />
          <textarea
            className="min-h-[56px] rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
            placeholder="Description (optional)"
            value={editDescription}
            onChange={(e) => setEditDescription(e.target.value)}
          />
          <select
            value={editType}
            onChange={(e) => setEditType(e.target.value as Exclude<TicketType, "subtask">)}
            className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink"
          >
            {PARSE_TYPE_OPTIONS.map((t) => (
              <option key={t} value={t}>
                {t[0].toUpperCase() + t.slice(1)}
              </option>
            ))}
          </select>

          {(candidate.priority || candidate.labels.length > 0) && (
            <div className="flex flex-wrap items-center gap-1 text-[11px] text-ink-tertiary">
              <span>AI preview (finalized after creation):</span>
              {candidate.priority && <PriorityBadge priority={candidate.priority} />}
              {candidate.labels.map((label) => (
                <span key={label} className="rounded-sm bg-hover px-1.5 py-0.5 font-mono">
                  {label}
                </span>
              ))}
            </div>
          )}

          <DuplicateWarning projectId={projectId} ticketPrefix={ticketPrefix} matches={duplicates} />

          {error && <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>}
          <div className="flex items-center gap-2">
            <button
              type="submit"
              disabled={confirming}
              className="self-start rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
            >
              {confirming ? "Creating…" : duplicates.length > 0 ? "Create anyway" : "Create ticket"}
            </button>
            <button
              type="button"
              onClick={() => setCandidate(null)}
              className="self-start rounded-md px-3 py-1.5 text-sm font-medium text-ink-secondary hover:text-ink"
            >
              Back
            </button>
            {/* Edits to title/description above don't auto-re-check --
                that would need debouncing to avoid a request per
                keystroke. This lets a user who changed their mind about
                the wording re-check on demand instead. */}
            <button
              type="button"
              disabled={checkingDuplicates}
              onClick={() => runDuplicateCheck(editTitle, editDescription || null)}
              className="self-start text-xs text-ink-tertiary underline hover:text-ink-secondary disabled:opacity-50"
            >
              {checkingDuplicates ? "Checking…" : "Check again"}
            </button>
          </div>
        </form>
      )}
    </div>
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
      <TriageIndicator ticket={ticket} />
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
  const [activeForm, setActiveForm] = useState<"quick" | "describe" | null>(null);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [checkingDuplicates, setCheckingDuplicates] = useState(false);
  const [duplicates, setDuplicates] = useState<DuplicateCandidate[]>([]);

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

  // Live board updates: a workflow_state_id/position/assignee/sprint/
  // story_points change made by anyone else connected to this project
  // arrives here and is spliced directly into local state, the same
  // "instant local update, no round trip" reasoning as handleDragEnd's own
  // optimistic move below -- just triggered by someone else's action
  // instead of this tab's own drag. updatedBy === user.id is skipped: this
  // tab already applied its own change optimistically the moment it made
  // it, so re-applying the echoed event would be redundant at best and a
  // stale overwrite at worst if a second local action happened before the
  // echo arrived. setTickets uses the functional form specifically so this
  // handler never closes over a stale `tickets` snapshot from whenever the
  // effect last ran -- it only re-runs on [projectId, user?.id], not on
  // every ticket change. A ticket id not found in local state (filtered
  // out of the current view, e.g. a future sprint filter) is a silent
  // no-op, not an error -- there's nothing to splice the change into, and
  // a full refetch would defeat the point of pushing a diff at all.
  useEffect(() => {
    let socket: WebSocket | null = null;
    let cancelled = false;

    (async () => {
      const ws = await connectProjectSocket(projectId);
      if (cancelled) {
        ws?.close();
        return;
      }
      if (!ws) {
        console.warn("[ws] could not obtain a connection ticket");
        return;
      }
      socket = ws;
      ws.onopen = () => console.log("[ws] connected to project", projectId);
      ws.onclose = (e) => console.log("[ws] closed", e.code, e.reason);
      ws.onerror = () => console.log("[ws] error");
      ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        if (data.type !== "ticket.updated") return;
        const event = mapTicketUpdatedEvent(data);
        if (event.updatedBy === user?.id) return;
        setTickets((prev) =>
          prev?.map((t) => (t.id === event.ticketId ? { ...t, ...event.changes } : t)) ?? prev,
        );
      };
    })();

    return () => {
      cancelled = true;
      socket?.close();
    };
  }, [projectId, user?.id]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    // First submit: check for duplicates instead of creating right away
    // -- if any are found, show them and wait for a second, explicit
    // "Create anyway" click rather than silently creating. Once
    // `duplicates` is populated for the *current* title, this branch is
    // skipped and the ticket is created directly -- same non-blocking
    // "let the user proceed or reconsider" shape as the NL-parse review
    // step, just compressed into one form instead of two steps.
    if (duplicates.length === 0) {
      setCheckingDuplicates(true);
      const found = await checkDuplicates(projectId, title, null);
      setCheckingDuplicates(false);
      if (found.length > 0) {
        setDuplicates(found);
        return;
      }
    }

    setSubmitting(true);
    const res = await apiFetch(`/api/projects/${projectId}/tickets`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "task", title, description: null }),
    });

    if (res.ok) {
      setTitle("");
      setDuplicates([]);
      setActiveForm(null);
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
            href={`/projects/${projectId}/backlog`}
            className="rounded-md border border-line px-3 py-1.5 text-sm font-medium text-ink transition-colors duration-100 hover:border-line-strong hover:bg-hover"
          >
            Backlog
          </a>
          <a
            href={`/projects/${projectId}/sprints`}
            className="rounded-md border border-line px-3 py-1.5 text-sm font-medium text-ink transition-colors duration-100 hover:border-line-strong hover:bg-hover"
          >
            Sprints
          </a>
          <a
            href={`/projects/${projectId}/settings`}
            className="rounded-md border border-line px-3 py-1.5 text-sm font-medium text-ink transition-colors duration-100 hover:border-line-strong hover:bg-hover"
          >
            Workflow settings
          </a>
          <button
            onClick={() => setActiveForm((v) => (v === "describe" ? null : "describe"))}
            className="rounded-md border border-line px-3 py-1.5 text-sm font-medium text-ink transition-colors duration-100 hover:border-line-strong hover:bg-hover"
          >
            {activeForm === "describe" ? "Cancel" : "Describe with AI"}
          </button>
          <button
            onClick={() => setActiveForm((v) => (v === "quick" ? null : "quick"))}
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
          >
            {activeForm === "quick" ? "Cancel" : "New ticket"}
          </button>
        </div>
      </div>

      {activeForm === "quick" && (
        <form
          onSubmit={handleCreate}
          className="flex w-full max-w-md flex-col gap-2.5 rounded-lg border border-line bg-surface p-3"
        >
          <input
            className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
            placeholder="Ticket title"
            value={title}
            onChange={(e) => {
              setTitle(e.target.value);
              // The title changed since duplicates (if any) were
              // computed -- those results no longer describe what's
              // about to be submitted, so the next submit should check
              // again rather than silently reuse a stale warning (or
              // worse, a stale *absence* of one).
              setDuplicates([]);
            }}
            required
          />
          <DuplicateWarning
            projectId={projectId}
            ticketPrefix={user?.ticketPrefix ?? "TKT"}
            matches={duplicates}
          />
          {error && (
            <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>
          )}
          <button
            type="submit"
            disabled={submitting || checkingDuplicates}
            className="self-start rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
          >
            {submitting
              ? "Creating…"
              : checkingDuplicates
                ? "Checking…"
                : duplicates.length > 0
                  ? "Create anyway"
                  : "Create"}
          </button>
        </form>
      )}

      {activeForm === "describe" && (
        <ParseTicketPanel
          projectId={projectId}
          ticketPrefix={user?.ticketPrefix ?? "TKT"}
          onCreated={async () => {
            setActiveForm(null);
            await load();
          }}
          onCancel={() => setActiveForm(null)}
        />
      )}

      {error && activeForm === null && (
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
          <div className="mt-1 flex gap-2">
            <button
              onClick={() => setActiveForm("describe")}
              className="rounded-md border border-line px-3.5 py-1.5 text-sm font-medium text-ink hover:border-line-strong hover:bg-hover"
            >
              Describe with AI
            </button>
            <button
              onClick={() => setActiveForm("quick")}
              className="rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
            >
              New ticket
            </button>
          </div>
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
