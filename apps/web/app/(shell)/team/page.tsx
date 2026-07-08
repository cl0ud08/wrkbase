"use client";

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "../../../lib/api";
import { useAuth } from "../../../lib/auth-context";
import {
  mapInvite,
  mapInviteCreateResult,
  mapMember,
  type Invite,
  type InviteCreateResult,
  type Member,
  type Role,
} from "../../../lib/types";

function initials(email: string): string {
  return email.slice(0, 2).toUpperCase();
}

function inviteStatus(invite: Invite): "accepted" | "expired" | "pending" {
  if (invite.acceptedAt) return "accepted";
  if (new Date(invite.expiresAt).getTime() < Date.now()) return "expired";
  return "pending";
}

const STATUS_CHIP: Record<string, string> = {
  accepted: "bg-success-bg text-success",
  pending: "bg-info-bg text-info",
  expired: "bg-warning-bg text-warning",
};

const ROLE_CHIP: Record<Role, string> = {
  admin: "bg-info-bg text-info",
  member: "bg-neutral-bg text-neutral",
  viewer: "bg-neutral-bg text-neutral",
};

function Dot() {
  return <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />;
}

export default function TeamPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  const [members, setMembers] = useState<Member[] | null>(null);
  const [invites, setInvites] = useState<Invite[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [inviting, setInviting] = useState(false);
  const [lastInvite, setLastInvite] = useState<InviteCreateResult | null>(null);

  const loadMembers = useCallback(async () => {
    const res = await apiFetch("/api/org/members");
    if (res.ok) setMembers((await res.json()).map(mapMember));
  }, []);

  const loadInvites = useCallback(async () => {
    if (!isAdmin) return;
    const res = await apiFetch("/api/invites");
    if (res.ok) setInvites((await res.json()).map(mapInvite));
  }, [isAdmin]);

  useEffect(() => {
    (async () => {
      await Promise.all([loadMembers(), loadInvites()]);
    })();
  }, [loadMembers, loadInvites]);

  async function handleInvite(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setInviting(true);
    setLastInvite(null);

    const res = await apiFetch("/api/invites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, role }),
    });

    if (res.ok) {
      setLastInvite(mapInviteCreateResult(await res.json()));
      setEmail("");
      setRole("member");
      await loadInvites();
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to create invite.");
    }
    setInviting(false);
  }

  async function handleRoleChange(memberId: string, newRole: Role) {
    setError(null);
    const res = await apiFetch(`/api/org/members/${memberId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: newRole }),
    });
    if (res.ok) {
      await loadMembers();
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to change role.");
    }
  }

  async function handleRemove(memberId: string) {
    setError(null);
    const res = await apiFetch(`/api/org/members/${memberId}`, { method: "DELETE" });
    if (res.ok) {
      await loadMembers();
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to remove member.");
    }
  }

  async function handleRevoke(inviteId: string) {
    setError(null);
    const res = await apiFetch(`/api/invites/${inviteId}`, { method: "DELETE" });
    if (res.ok) {
      await loadInvites();
    } else {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Failed to revoke invite.");
    }
  }

  return (
    <div className="flex flex-1 flex-col items-center px-4 py-10">
      <div className="flex w-full max-w-2xl flex-col gap-6">
        <h1 className="text-lg font-bold tracking-tight text-ink">Team</h1>

        {error && (
          <p className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>
        )}

        {isAdmin && (
          <div className="flex flex-col gap-3 rounded-lg border border-line bg-surface p-4">
            <h2 className="text-sm font-semibold text-ink">Invite someone</h2>
            <form onSubmit={handleInvite} className="flex flex-wrap items-end gap-2.5">
              <label className="flex min-w-[200px] flex-1 flex-col gap-1.5 text-xs text-ink-secondary">
                Email
                <input
                  type="email"
                  className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                />
              </label>
              <label className="flex flex-col gap-1.5 text-xs text-ink-secondary">
                Role
                <select
                  className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm text-ink transition-colors duration-100 hover:border-line-strong"
                  value={role}
                  onChange={(e) => setRole(e.target.value as Role)}
                >
                  <option value="admin">admin</option>
                  <option value="member">member</option>
                  <option value="viewer">viewer</option>
                </select>
              </label>
              <button
                type="submit"
                disabled={inviting}
                className="rounded-md bg-accent px-3.5 py-2 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover disabled:opacity-50"
              >
                {inviting ? "Sending…" : "Invite"}
              </button>
            </form>

            {lastInvite && (
              <div className="rounded-md border border-accent bg-accent-subtle p-3 text-sm">
                <p className="text-accent-subtle-text">
                  Emailing invites isn&apos;t wired up yet — copy this link and share it manually:
                </p>
                <p className="mt-1 truncate font-mono text-xs text-ink">{lastInvite.link}</p>
              </div>
            )}
          </div>
        )}

        <div className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold text-ink">Members</h2>
          {members === null && <p className="text-sm text-ink-tertiary">Loading…</p>}
          <div className="flex flex-col overflow-hidden rounded-lg border border-line">
            {members?.map((member, i) => (
              <div
                key={member.id}
                className={`flex items-center justify-between gap-3 bg-surface px-3 py-2.5 ${
                  i !== 0 ? "border-t border-line-subtle" : ""
                }`}
              >
                <div className="flex min-w-0 items-center gap-2.5">
                  <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-[4px] border border-line bg-hover font-mono text-[10px] font-semibold text-ink-secondary">
                    {initials(member.email)}
                  </span>
                  <span className="truncate text-sm text-ink">{member.email}</span>
                </div>
                {isAdmin ? (
                  <div className="flex flex-shrink-0 items-center gap-2">
                    <select
                      className="rounded-md border border-line bg-surface-2 px-2 py-1 text-xs text-ink transition-colors duration-100 hover:border-line-strong"
                      value={member.role}
                      onChange={(e) => handleRoleChange(member.id, e.target.value as Role)}
                    >
                      <option value="admin">admin</option>
                      <option value="member">member</option>
                      <option value="viewer">viewer</option>
                    </select>
                    <button
                      onClick={() => handleRemove(member.id)}
                      className="rounded-md border border-line px-2.5 py-1 text-xs font-medium text-danger transition-colors duration-100 hover:border-danger hover:bg-danger-bg"
                    >
                      Remove
                    </button>
                  </div>
                ) : (
                  <span
                    className={`flex flex-shrink-0 items-center gap-1.5 rounded-sm px-2 py-0.5 text-xs font-medium ${ROLE_CHIP[member.role]}`}
                  >
                    <Dot />
                    {member.role}
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>

        {isAdmin && (
          <div className="flex flex-col gap-2">
            <h2 className="text-sm font-semibold text-ink">Invites</h2>
            {invites !== null && invites.length === 0 && (
              <p className="text-sm text-ink-tertiary">No invites yet.</p>
            )}
            {invites !== null && invites.length > 0 && (
              <div className="flex flex-col overflow-hidden rounded-lg border border-line">
                {invites.map((invite, i) => {
                  const status = inviteStatus(invite);
                  return (
                    <div
                      key={invite.id}
                      className={`flex items-center justify-between gap-3 bg-surface px-3 py-2.5 ${
                        i !== 0 ? "border-t border-line-subtle" : ""
                      }`}
                    >
                      <div className="flex min-w-0 flex-col">
                        <span className="truncate text-sm text-ink">{invite.email}</span>
                        <span className="text-xs text-ink-tertiary">{invite.role}</span>
                      </div>
                      <div className="flex flex-shrink-0 items-center gap-2">
                        <span
                          className={`flex items-center gap-1.5 rounded-sm px-2 py-0.5 text-xs font-medium ${STATUS_CHIP[status]}`}
                        >
                          <Dot />
                          {status}
                        </span>
                        {status === "pending" && (
                          <button
                            onClick={() => handleRevoke(invite.id)}
                            className="rounded-md border border-line px-2.5 py-1 text-xs font-medium text-danger transition-colors duration-100 hover:border-danger hover:bg-danger-bg"
                          >
                            Revoke
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
