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
    createdAt: data.created_at,
    updatedAt: data.updated_at,
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
