"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { apiFetch } from "../lib/api";
import {
  mapNotification,
  mapNotificationCreatedEvent,
  mapNotificationPage,
  type Notification,
} from "../lib/types";
import { connectNotificationSocket } from "../lib/ws";

// Rough, not exact -- a notification panel doesn't need second-precision,
// and pulling in a date library for "5m ago" would be a real dependency
// for a one-line formatter this app hasn't needed anywhere else yet.
function relativeTime(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// payload is type-specific (see apps/api/app/services/notifications.py) --
// this is the one place that shape is actually consumed, so it's read
// directly here rather than adding a discriminated-union type nothing
// else in the frontend needs yet.
function describe(n: Notification): { text: string; href: string | null } {
  switch (n.type) {
    case "assignment":
      return {
        text: `You were assigned "${n.payload.ticket_title as string}"`,
        href: `/projects/${n.payload.project_id as string}`,
      };
    case "invite_accepted":
      return {
        text: `${n.payload.accepted_email as string} accepted your invite as ${n.payload.role as string}`,
        href: "/team",
      };
    case "mention":
      return { text: "You mentioned in a comment", href: null };
  }
}

export default function NotificationBell() {
  const [unreadCount, setUnreadCount] = useState(0);
  const [open, setOpen] = useState(false);
  const [notifications, setNotifications] = useState<Notification[] | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const loadUnreadCount = useCallback(async () => {
    const res = await apiFetch("/api/notifications/unread-count");
    if (res.ok) setUnreadCount((await res.json()).count);
  }, []);

  const loadNotifications = useCallback(async () => {
    const res = await apiFetch("/api/notifications?limit=20");
    if (res.ok) setNotifications(mapNotificationPage(await res.json()).items);
  }, []);

  useEffect(() => {
    (async () => {
      await loadUnreadCount();
    })();
  }, [loadUnreadCount]);

  // One connection for the lifetime of this component (mounted once, in
  // the shell layout, alive across every authenticated page) -- not tied
  // to which project's board happens to be open, since a notification has
  // to arrive regardless. See lib/ws.ts's connectNotificationSocket.
  useEffect(() => {
    let socket: WebSocket | null = null;
    let cancelled = false;

    (async () => {
      const ws = await connectNotificationSocket();
      if (cancelled) {
        ws?.close();
        return;
      }
      if (!ws) return;
      socket = ws;
      ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        if (data.type !== "notification.created") return;
        const event = mapNotificationCreatedEvent(data);
        setUnreadCount((c) => c + 1);
        setNotifications((prev) => (prev === null ? prev : [event.notification, ...prev]));
      };
    })();

    return () => {
      cancelled = true;
      socket?.close();
    };
  }, []);

  // Close on outside click -- a dropdown that only closes via its own
  // toggle button is a common small annoyance this avoids for one
  // listener's worth of code.
  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  async function handleToggle() {
    const opening = !open;
    setOpen(opening);
    if (opening && notifications === null) await loadNotifications();
  }

  async function handleMarkAllRead() {
    const res = await apiFetch("/api/notifications/read-all", { method: "POST" });
    if (res.ok) {
      setUnreadCount(0);
      setNotifications((prev) => prev?.map((n) => ({ ...n, readAt: n.readAt ?? new Date().toISOString() })) ?? prev);
    }
  }

  async function handleMarkRead(id: string) {
    const res = await apiFetch(`/api/notifications/${id}/read`, { method: "PATCH" });
    if (res.ok) {
      const updated = mapNotification(await res.json());
      setNotifications((prev) => prev?.map((n) => (n.id === id ? updated : n)) ?? prev);
      setUnreadCount((c) => Math.max(0, c - 1));
    }
  }

  return (
    <div ref={panelRef} className="relative">
      <button
        type="button"
        onClick={handleToggle}
        aria-label={unreadCount > 0 ? `${unreadCount} unread notifications` : "Notifications"}
        className="relative flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-md border border-line text-ink-secondary transition-colors duration-100 hover:border-line-strong hover:text-ink"
      >
        <svg viewBox="0 0 16 16" fill="none" className="h-3.5 w-3.5" aria-hidden="true">
          <path
            d="M8 1.5c-2 0-3.5 1.5-3.5 3.75v2.1c0 .5-.2 1-.55 1.35L3 9.75V11h10V9.75l-.95-1.1c-.35-.35-.55-.85-.55-1.35v-2.1C11.5 3 10 1.5 8 1.5Z"
            stroke="currentColor"
            strokeWidth="1.1"
            strokeLinejoin="round"
          />
          <path d="M6.3 11.5a1.7 1.7 0 0 0 3.4 0" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
        </svg>
        {unreadCount > 0 && (
          <span className="absolute -top-1 -right-1 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-accent px-1 font-mono text-[9px] font-semibold text-accent-on">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute top-9 right-0 z-30 flex max-h-[70vh] w-80 flex-col overflow-hidden rounded-lg border border-line bg-surface shadow-[var(--shadow-elevated)]">
          <div className="flex flex-shrink-0 items-center justify-between border-b border-line-subtle px-3 py-2">
            <h2 className="text-xs font-semibold text-ink">Notifications</h2>
            {unreadCount > 0 && (
              <button
                onClick={handleMarkAllRead}
                className="text-xs font-medium text-ink-tertiary hover:text-accent"
              >
                Mark all read
              </button>
            )}
          </div>
          <div className="flex-1 overflow-y-auto">
            {notifications === null && (
              <p className="px-3 py-4 text-center text-xs text-ink-tertiary">Loading…</p>
            )}
            {notifications !== null && notifications.length === 0 && (
              <p className="px-3 py-4 text-center text-xs text-ink-tertiary">No notifications yet.</p>
            )}
            {notifications?.map((n) => {
              const { text, href } = describe(n);
              const unread = n.readAt === null;
              const content = (
                <div
                  className={`flex flex-col gap-0.5 border-b border-line-subtle px-3 py-2.5 text-sm transition-colors duration-100 hover:bg-hover ${
                    unread ? "bg-accent-subtle/40" : ""
                  }`}
                >
                  <div className="flex items-start gap-2">
                    {unread && (
                      <span className="mt-1.5 h-1.5 w-1.5 flex-shrink-0 rounded-full bg-accent" aria-hidden="true" />
                    )}
                    <p className={`flex-1 leading-snug ${unread ? "text-ink" : "text-ink-secondary"}`}>{text}</p>
                  </div>
                  <span className="pl-3.5 text-xs text-ink-tertiary">{relativeTime(n.createdAt)}</span>
                </div>
              );
              return (
                <a
                  key={n.id}
                  href={href ?? undefined}
                  onClick={() => unread && handleMarkRead(n.id)}
                  className={href ? "block" : "block cursor-default"}
                >
                  {content}
                </a>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
