export interface Project {
  id: string;
  orgId: string;
  name: string;
  description: string | null;
  // Nullable: a removed member's projects are kept with created_by set to
  // NULL (migration 0008) — see apps/api/app/api/org.py.
  createdBy: string | null;
  createdAt: string;
  updatedAt: string;
}

// Shape the API actually returns (snake_case, matches ProjectRead in
// apps/api/app/schemas/project.py) — mapped to the camelCase Project above
// at the fetch boundary, same convention as auth-context.tsx.
interface ProjectApiResponse {
  id: string;
  org_id: string;
  name: string;
  description: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export function mapProject(data: ProjectApiResponse): Project {
  return {
    id: data.id,
    orgId: data.org_id,
    name: data.name,
    description: data.description,
    createdBy: data.created_by,
    createdAt: data.created_at,
    updatedAt: data.updated_at,
  };
}

export type TicketType = "epic" | "story" | "task" | "subtask";
export type TicketPriority = "low" | "medium" | "high" | "critical";
export type TriageStatus = "pending" | "triaged" | "failed";
export type AppSecReviewStatus = "pending" | "completed" | "failed";

export interface Ticket {
  id: string;
  orgId: string;
  projectId: string;
  parentId: string | null;
  type: TicketType;
  title: string;
  description: string | null;
  workflowStateId: string;
  position: number;
  createdBy: string | null;
  // Nullable: unassigned, or a removed member's old assignment (SET NULL
  // on member removal, same as createdBy) — see migration 0009.
  assigneeId: string | null;
  // Combine with the current org's ticketPrefix (see lib/auth-context.tsx)
  // to render a ticket's display key, e.g. "WRK-142".
  ticketNumber: number;
  // null = in the backlog (see fetchBacklog / mapSprint below).
  sprintId: string | null;
  storyPoints: number | null;
  // The authoritative triage state — not "both null" anymore (see
  // apps/api/app/db/models.py's Ticket.triage_status docstring): a real
  // LLM call (apps/api/app/services/llm_triage.py) can fail outright
  // after both providers are exhausted, a genuine third outcome the old
  // two-nullable-fields idiom couldn't express. The board renders "AI
  // triaging…" for pending, real priority/labels for triaged, and a
  // distinct failed indicator for failed — all via the same live update
  // mechanism, no polling.
  triageStatus: TriageStatus;
  priority: TicketPriority | null;
  labels: string[] | null;
  triageReasoning: string | null;
  triageError: string | null;
  triagedAt: string | null;
  // NULL for the vast majority of tickets — no AppSec trigger category
  // has ever matched (apps/api/app/services/appsec_triggers.py). Set to
  // "pending" synchronously the instant a keyword match happens, then
  // flipped to "completed"/"failed" by the async review worker — see
  // apps/api/app/services/appsec_review.py.
  appsecReviewStatus: AppSecReviewStatus | null;
  appsecCategories: string[] | null;
  appsecComment: string | null;
  appsecReviewError: string | null;
  appsecReviewedAt: string | null;
  createdAt: string;
  updatedAt: string;
  // Computed at read time, never stored (see
  // apps/api/app/services/at_risk.py's module docstring) — null for
  // every ticket not currently in its project's active sprint, since
  // risk only means anything relative to an active sprint's own
  // deadline. Only actually populated by GET .../tickets and GET
  // .../tickets/{id}; every other ticket response leaves these null.
  atRisk: boolean | null;
  atRiskReasons: string[] | null;
}

// No TicketTreeNode type yet: nothing on the frontend consumes the /tree
// endpoint this slice (flat list only, per the brief) — adding it now
// would be a type with no caller. The next slice (hierarchy rendering)
// adds it alongside the UI that actually needs it.
interface TicketApiResponse {
  id: string;
  org_id: string;
  project_id: string;
  parent_id: string | null;
  type: TicketType;
  title: string;
  description: string | null;
  workflow_state_id: string;
  position: number;
  created_by: string | null;
  assignee_id: string | null;
  ticket_number: number;
  sprint_id: string | null;
  story_points: number | null;
  triage_status: TriageStatus;
  priority: TicketPriority | null;
  labels: string[] | null;
  triage_reasoning: string | null;
  triage_error: string | null;
  triaged_at: string | null;
  appsec_review_status: AppSecReviewStatus | null;
  appsec_categories: string[] | null;
  appsec_comment: string | null;
  appsec_review_error: string | null;
  appsec_reviewed_at: string | null;
  created_at: string;
  updated_at: string;
  at_risk: boolean | null;
  at_risk_reasons: string[] | null;
}

export function mapTicket(data: TicketApiResponse): Ticket {
  return {
    id: data.id,
    orgId: data.org_id,
    projectId: data.project_id,
    parentId: data.parent_id,
    type: data.type,
    title: data.title,
    description: data.description,
    workflowStateId: data.workflow_state_id,
    position: data.position,
    createdBy: data.created_by,
    assigneeId: data.assignee_id,
    ticketNumber: data.ticket_number,
    sprintId: data.sprint_id,
    storyPoints: data.story_points,
    triageStatus: data.triage_status,
    priority: data.priority,
    labels: data.labels,
    triageReasoning: data.triage_reasoning,
    triageError: data.triage_error,
    triagedAt: data.triaged_at,
    appsecReviewStatus: data.appsec_review_status,
    appsecCategories: data.appsec_categories,
    appsecComment: data.appsec_comment,
    appsecReviewError: data.appsec_review_error,
    appsecReviewedAt: data.appsec_reviewed_at,
    createdAt: data.created_at,
    updatedAt: data.updated_at,
    atRisk: data.at_risk,
    atRiskReasons: data.at_risk_reasons,
  };
}

// Offset pagination — matches TicketPage in apps/api/app/schemas/ticket.py.
export interface TicketPage {
  items: Ticket[];
  total: number;
  limit: number;
  offset: number;
}

interface TicketPageApiResponse {
  items: TicketApiResponse[];
  total: number;
  limit: number;
  offset: number;
}

export function mapTicketPage(data: TicketPageApiResponse): TicketPage {
  return {
    items: data.items.map(mapTicket),
    total: data.total,
    limit: data.limit,
    offset: data.offset,
  };
}

// Returned by POST .../tickets/check-duplicates
// (apps/api/app/services/ticket_duplicates.py). A non-blocking signal --
// see the project board's DuplicateWarning component, which never
// prevents a creation form from submitting, only shows what it found.
export interface DuplicateCandidate {
  ticketId: string;
  ticketNumber: number;
  title: string;
  similarity: number;
}

interface DuplicateCandidateApiResponse {
  ticket_id: string;
  ticket_number: number;
  title: string;
  similarity: number;
}

interface DuplicateCheckResponseApi {
  matches: DuplicateCandidateApiResponse[];
}

export function mapDuplicateCandidates(data: DuplicateCheckResponseApi): DuplicateCandidate[] {
  return data.matches.map((m) => ({
    ticketId: m.ticket_id,
    ticketNumber: m.ticket_number,
    title: m.title,
    similarity: m.similarity,
  }));
}

// Returned by POST .../tickets/parse (apps/api/app/services/ticket_parse.py)
// -- never persisted, never a Ticket. confident=true guarantees title and
// type are both present; confident=false guarantees clarification is
// present instead and means the frontend should NOT pre-fill a review
// form from the other fields, even if some are non-null — the model may
// have offered partial guesses, but nothing here treats them as reliable
// (see ticket_parse.py's own docstring for why). priority/labels are a
// preview only: confirming a candidate submits just type/title/description
// through the normal create-ticket endpoint, which still runs real async
// triage afterward — that, not this preview, is what actually ends up on
// the created ticket.
export interface ParsedTicketCandidate {
  confident: boolean;
  title: string | null;
  description: string | null;
  type: TicketType | null;
  priority: TicketPriority | null;
  labels: string[];
  clarification: string | null;
}

interface ParsedTicketCandidateApiResponse {
  confident: boolean;
  title: string | null;
  description: string | null;
  type: TicketType | null;
  priority: TicketPriority | null;
  labels: string[];
  clarification: string | null;
}

export function mapParsedTicketCandidate(
  data: ParsedTicketCandidateApiResponse,
): ParsedTicketCandidate {
  return {
    confident: data.confident,
    title: data.title,
    description: data.description,
    type: data.type,
    priority: data.priority,
    labels: data.labels,
    clarification: data.clarification,
  };
}

// Pushed over the project WebSocket (see apps/api/app/services/ticket_events.py)
// whenever a collaborative field changes — a deliberately minimal diff, not
// a full Ticket, so a connected client can splice it into local state
// without a round trip. Only ever the subset of fields that actually
// changed, hence Partial rather than every field being required.
export interface TicketUpdatedEvent {
  type: "ticket.updated";
  projectId: string;
  ticketId: string;
  changes: Partial<
    Pick<
      Ticket,
      | "workflowStateId"
      | "position"
      | "assigneeId"
      | "sprintId"
      | "storyPoints"
      | "triageStatus"
      | "priority"
      | "labels"
      | "triageReasoning"
      | "triageError"
      | "triagedAt"
      | "appsecReviewStatus"
      | "appsecComment"
      | "appsecReviewError"
      | "appsecReviewedAt"
    >
  >;
  // The acting user's id — lets a receiving client tell "someone else moved
  // this" apart from its own change echoing back, without needing the
  // server to suppress the echo itself (every subscriber gets every
  // event, uniformly, including the one who caused it). The worker
  // publishes its own triage-complete update with a fixed all-zero
  // sentinel here (see apps/api/app/services/ticket_events.py's
  // SYSTEM_ACTOR_ID) since there's no human actor to attribute it to —
  // it can never equal a real user.id, so it's never treated as an echo.
  updatedBy: string;
}

interface TicketUpdatedEventApiPayload {
  type: "ticket.updated";
  project_id: string;
  ticket_id: string;
  changes: {
    workflow_state_id?: string;
    position?: number;
    assignee_id?: string | null;
    sprint_id?: string | null;
    story_points?: number | null;
    triage_status?: TriageStatus;
    priority?: TicketPriority;
    labels?: string[];
    triage_reasoning?: string;
    triage_error?: string;
    triaged_at?: string;
    appsec_review_status?: AppSecReviewStatus;
    appsec_comment?: string;
    appsec_review_error?: string;
    appsec_reviewed_at?: string;
  };
  updated_by: string;
}

export function mapTicketUpdatedEvent(data: TicketUpdatedEventApiPayload): TicketUpdatedEvent {
  const changes: TicketUpdatedEvent["changes"] = {};
  if ("workflow_state_id" in data.changes) changes.workflowStateId = data.changes.workflow_state_id;
  if ("position" in data.changes) changes.position = data.changes.position;
  if ("assignee_id" in data.changes) changes.assigneeId = data.changes.assignee_id ?? null;
  if ("sprint_id" in data.changes) changes.sprintId = data.changes.sprint_id ?? null;
  if ("story_points" in data.changes) changes.storyPoints = data.changes.story_points ?? null;
  if ("triage_status" in data.changes) changes.triageStatus = data.changes.triage_status;
  if ("priority" in data.changes) changes.priority = data.changes.priority;
  if ("labels" in data.changes) changes.labels = data.changes.labels;
  if ("triage_reasoning" in data.changes) changes.triageReasoning = data.changes.triage_reasoning;
  if ("triage_error" in data.changes) changes.triageError = data.changes.triage_error;
  if ("triaged_at" in data.changes) changes.triagedAt = data.changes.triaged_at;
  if ("appsec_review_status" in data.changes) changes.appsecReviewStatus = data.changes.appsec_review_status;
  if ("appsec_comment" in data.changes) changes.appsecComment = data.changes.appsec_comment;
  if ("appsec_review_error" in data.changes) changes.appsecReviewError = data.changes.appsec_review_error;
  if ("appsec_reviewed_at" in data.changes) changes.appsecReviewedAt = data.changes.appsec_reviewed_at;
  return {
    type: data.type,
    projectId: data.project_id,
    ticketId: data.ticket_id,
    changes,
    updatedBy: data.updated_by,
  };
}

export type SprintStatus = "planned" | "active" | "completed";

// pending/completed/failed — see apps/api/app/services/sprint_retro.py
// and apps/api/worker/main.py. NULL only for a sprint that hasn't
// completed yet (a real, permanent state, not a transient "not started").
export type SprintRetroStatus = "pending" | "completed" | "failed";

export interface Sprint {
  id: string;
  orgId: string;
  projectId: string;
  name: string;
  goal: string | null;
  startDate: string;
  endDate: string;
  status: SprintStatus;
  // Computed server-side (sum of story_points for the sprint's current
  // tickets) — see apps/api/app/api/sprints.py. Never derive this by
  // summing a fetched ticket list client-side; the backend is the only
  // place that's guaranteed consistent with what a ticket list endpoint
  // would return at the same instant.
  totalPoints: number;
  // Computed server-side, only once the sprint has completed (NULL
  // before that — see apps/api/app/api/sprints.py's _points_planned):
  // totalPoints (what finished) plus whatever was captured in the
  // returned-ticket snapshot at the moment of completion.
  pointsPlanned: number | null;
  // Computed server-side, only for the currently ACTIVE sprint (null for
  // planned/completed — see apps/api/app/services/at_risk.py). Never
  // derive this by counting atRisk client-side across a fetched ticket
  // list; same "backend is the only place guaranteed consistent"
  // reasoning as totalPoints above.
  atRiskCount: number | null;
  createdAt: string;
  retroStatus: SprintRetroStatus | null;
  retroNarrative: string | null;
  retroCompletedHighlights: string[] | null;
  retroIncompleteNotes: string[] | null;
  retroRisks: string[] | null;
  retroError: string | null;
  retroGeneratedAt: string | null;
}

interface SprintApiResponse {
  id: string;
  org_id: string;
  project_id: string;
  name: string;
  goal: string | null;
  start_date: string;
  end_date: string;
  status: SprintStatus;
  total_points: number;
  points_planned: number | null;
  at_risk_count: number | null;
  created_at: string;
  retro_status: SprintRetroStatus | null;
  retro_narrative: string | null;
  retro_completed_highlights: string[] | null;
  retro_incomplete_notes: string[] | null;
  retro_risks: string[] | null;
  retro_error: string | null;
  retro_generated_at: string | null;
}

export function mapSprint(data: SprintApiResponse): Sprint {
  return {
    id: data.id,
    orgId: data.org_id,
    projectId: data.project_id,
    name: data.name,
    goal: data.goal,
    startDate: data.start_date,
    endDate: data.end_date,
    status: data.status,
    totalPoints: data.total_points,
    pointsPlanned: data.points_planned,
    atRiskCount: data.at_risk_count,
    createdAt: data.created_at,
    retroStatus: data.retro_status,
    retroNarrative: data.retro_narrative,
    retroCompletedHighlights: data.retro_completed_highlights,
    retroIncompleteNotes: data.retro_incomplete_notes,
    retroRisks: data.retro_risks,
    retroError: data.retro_error,
    retroGeneratedAt: data.retro_generated_at,
  };
}

export interface WorkflowState {
  id: string;
  orgId: string;
  projectId: string;
  name: string;
  order: number;
  isDefault: boolean;
  createdAt: string;
}

interface WorkflowStateApiResponse {
  id: string;
  org_id: string;
  project_id: string;
  name: string;
  order: number;
  is_default: boolean;
  created_at: string;
}

export function mapWorkflowState(data: WorkflowStateApiResponse): WorkflowState {
  return {
    id: data.id,
    orgId: data.org_id,
    projectId: data.project_id,
    name: data.name,
    order: data.order,
    isDefault: data.is_default,
    createdAt: data.created_at,
  };
}

export type Role = "admin" | "member" | "viewer";

export interface Member {
  id: string;
  email: string;
  role: Role;
  createdAt: string;
}

interface MemberApiResponse {
  id: string;
  email: string;
  role: Role;
  created_at: string;
}

export function mapMember(data: MemberApiResponse): Member {
  return { id: data.id, email: data.email, role: data.role, createdAt: data.created_at };
}

export interface Invite {
  id: string;
  orgId: string;
  email: string;
  role: Role;
  invitedBy: string | null;
  expiresAt: string;
  acceptedAt: string | null;
  createdAt: string;
}

interface InviteApiResponse {
  id: string;
  org_id: string;
  email: string;
  role: Role;
  invited_by: string | null;
  expires_at: string;
  accepted_at: string | null;
  created_at: string;
}

export function mapInvite(data: InviteApiResponse): Invite {
  return {
    id: data.id,
    orgId: data.org_id,
    email: data.email,
    role: data.role,
    invitedBy: data.invited_by,
    expiresAt: data.expires_at,
    acceptedAt: data.accepted_at,
    createdAt: data.created_at,
  };
}

// Only present on the create response (see apps/api/app/schemas/invite.py's
// InviteCreateResponse) — shown once, right after creation, then never
// re-fetchable through GET /invites.
export interface InviteCreateResult extends Invite {
  token: string;
  link: string;
}

interface InviteCreateApiResponse extends InviteApiResponse {
  token: string;
  link: string;
}

export function mapInviteCreateResult(data: InviteCreateApiResponse): InviteCreateResult {
  return { ...mapInvite(data), token: data.token, link: data.link };
}

export type NotificationType = "assignment" | "invite_accepted" | "mention";

export interface Notification {
  id: string;
  type: NotificationType;
  // Deliberately untyped further than this: shape is type-specific (see
  // apps/api/app/services/notifications.py), and the only two consumers
  // (NotificationBell's renderer, keyed on `type`) already know which
  // fields to expect for the type they're rendering.
  payload: Record<string, unknown>;
  readAt: string | null;
  createdAt: string;
}

interface NotificationApiResponse {
  id: string;
  type: NotificationType;
  payload: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
}

export function mapNotification(data: NotificationApiResponse): Notification {
  return {
    id: data.id,
    type: data.type,
    payload: data.payload,
    readAt: data.read_at,
    createdAt: data.created_at,
  };
}

export interface NotificationPage {
  items: Notification[];
  total: number;
  limit: number;
  offset: number;
}

interface NotificationPageApiResponse {
  items: NotificationApiResponse[];
  total: number;
  limit: number;
  offset: number;
}

export function mapNotificationPage(data: NotificationPageApiResponse): NotificationPage {
  return {
    items: data.items.map(mapNotification),
    total: data.total,
    limit: data.limit,
    offset: data.offset,
  };
}

// Pushed over /ws/notifications (see apps/api/app/services/notifications.py)
// the instant a notification is created — the live-update counterpart to
// NotificationPage above.
export interface NotificationCreatedEvent {
  type: "notification.created";
  notification: Notification;
}

interface NotificationCreatedEventApiPayload {
  type: "notification.created";
  notification: NotificationApiResponse;
}

export function mapNotificationCreatedEvent(
  data: NotificationCreatedEventApiPayload,
): NotificationCreatedEvent {
  return { type: data.type, notification: mapNotification(data.notification) };
}
