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
  createdAt: string;
  updatedAt: string;
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
  created_at: string;
  updated_at: string;
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
    createdAt: data.created_at,
    updatedAt: data.updated_at,
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
    Pick<Ticket, "workflowStateId" | "position" | "assigneeId" | "sprintId" | "storyPoints">
  >;
  // The acting user's id — lets a receiving client tell "someone else moved
  // this" apart from its own change echoing back, without needing the
  // server to suppress the echo itself (every subscriber gets every
  // event, uniformly, including the one who caused it).
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
  return {
    type: data.type,
    projectId: data.project_id,
    ticketId: data.ticket_id,
    changes,
    updatedBy: data.updated_by,
  };
}

export type SprintStatus = "planned" | "active" | "completed";

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
  createdAt: string;
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
  created_at: string;
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
    createdAt: data.created_at,
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
