export interface Project {
  id: string;
  orgId: string;
  name: string;
  description: string | null;
  createdBy: string;
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
  created_by: string;
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
