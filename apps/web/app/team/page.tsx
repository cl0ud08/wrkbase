"use client";

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth-context";
import {
  mapInvite,
  mapInviteCreateResult,
  mapMember,
  type Invite,
  type InviteCreateResult,
  type Member,
  type Role,
} from "../../lib/types";

function inviteStatus(invite: Invite): "accepted" | "expired" | "pending" {
  if (invite.acceptedAt) return "accepted";
  if (new Date(invite.expiresAt).getTime() < Date.now()) return "expired";
  return "pending";
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
    <div className="flex flex-1 flex-col items-center gap-8 bg-zinc-50 px-6 py-16 font-sans dark:bg-black">
      <div className="w-full max-w-2xl">
        <a href="/dashboard" className="text-sm text-zinc-500 hover:underline dark:text-zinc-500">
          &larr; Projects
        </a>
        <h1 className="text-2xl font-semibold tracking-tight text-black dark:text-zinc-50">Team</h1>
        {user && <p className="text-sm text-zinc-500 dark:text-zinc-500">{user.orgName}</p>}
      </div>

      {error && <p className="w-full max-w-2xl text-sm text-red-500">{error}</p>}

      {isAdmin && (
        <div className="flex w-full max-w-2xl flex-col gap-3 rounded-xl border border-black/[.08] p-6 dark:border-white/[.145]">
          <h2 className="font-medium text-black dark:text-zinc-50">Invite someone</h2>
          <form onSubmit={handleInvite} className="flex flex-wrap items-end gap-3">
            <label className="flex flex-1 min-w-[200px] flex-col gap-1 text-sm">
              Email
              <input
                type="email"
                className="rounded-md border border-black/[.08] px-3 py-2 dark:border-white/[.145] dark:bg-zinc-900"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              Role
              <select
                className="rounded-md border border-black/[.08] px-3 py-2 dark:border-white/[.145] dark:bg-zinc-900"
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
              className="rounded-full bg-foreground px-5 py-2 text-sm font-medium text-background transition-colors hover:bg-[#383838] disabled:opacity-50 dark:hover:bg-[#ccc]"
            >
              {inviting ? "Sending..." : "Invite"}
            </button>
          </form>

          {lastInvite && (
            <div className="rounded-md border border-black/[.08] bg-black/[.03] p-3 text-sm dark:border-white/[.145] dark:bg-white/[.05]">
              <p className="text-zinc-600 dark:text-zinc-400">
                Emailing invites isn&apos;t wired up yet — copy this link and share it manually:
              </p>
              <p className="mt-1 break-all font-mono text-xs text-black dark:text-zinc-50">
                {lastInvite.link}
              </p>
            </div>
          )}
        </div>
      )}

      <div className="flex w-full max-w-2xl flex-col gap-3">
        <h2 className="font-medium text-black dark:text-zinc-50">Members</h2>
        {members === null && <p className="text-sm text-zinc-600 dark:text-zinc-400">Loading...</p>}
        {members?.map((member) => (
          <div
            key={member.id}
            className="flex items-center justify-between gap-3 rounded-xl border border-black/[.08] p-3 dark:border-white/[.145]"
          >
            <span className="text-sm text-black dark:text-zinc-50">{member.email}</span>
            {isAdmin ? (
              <div className="flex items-center gap-2">
                <select
                  className="rounded-md border border-black/[.08] px-2 py-1 text-sm dark:border-white/[.145] dark:bg-zinc-900"
                  value={member.role}
                  onChange={(e) => handleRoleChange(member.id, e.target.value as Role)}
                >
                  <option value="admin">admin</option>
                  <option value="member">member</option>
                  <option value="viewer">viewer</option>
                </select>
                <button
                  onClick={() => handleRemove(member.id)}
                  className="rounded-full border border-black/[.08] px-3 py-1 text-xs font-medium text-red-500 transition-colors hover:bg-red-500/10 dark:border-white/[.145]"
                >
                  Remove
                </button>
              </div>
            ) : (
              <span className="text-xs uppercase tracking-wide text-zinc-500 dark:text-zinc-500">
                {member.role}
              </span>
            )}
          </div>
        ))}
      </div>

      {isAdmin && (
        <div className="flex w-full max-w-2xl flex-col gap-3">
          <h2 className="font-medium text-black dark:text-zinc-50">Invites</h2>
          {invites !== null && invites.length === 0 && (
            <p className="text-sm text-zinc-600 dark:text-zinc-400">No invites yet.</p>
          )}
          {invites?.map((invite) => {
            const status = inviteStatus(invite);
            return (
              <div
                key={invite.id}
                className="flex items-center justify-between gap-3 rounded-xl border border-black/[.08] p-3 dark:border-white/[.145]"
              >
                <div className="flex flex-col">
                  <span className="text-sm text-black dark:text-zinc-50">{invite.email}</span>
                  <span className="text-xs text-zinc-500 dark:text-zinc-500">
                    {invite.role} &middot; {status}
                  </span>
                </div>
                {status === "pending" && (
                  <button
                    onClick={() => handleRevoke(invite.id)}
                    className="rounded-full border border-black/[.08] px-3 py-1 text-xs font-medium text-red-500 transition-colors hover:bg-red-500/10 dark:border-white/[.145]"
                  >
                    Revoke
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
