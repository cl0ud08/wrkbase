import { apiFetch } from "./api";

// The API's own origin, reachable directly from the browser -- deliberately
// NOT the same-origin /api/* rewrite proxy every other request in this app
// goes through. This project's pinned Next.js version documents no support
// for that proxy forwarding a WebSocket Upgrade request, so the socket
// connects straight to the API instead (see apps/api/app/api/ws.py's module
// docstring for the full reasoning, including why that also means the
// httpOnly cookie can't be relied on here and a minted ticket is used
// instead).
const WS_BASE_URL = process.env.NEXT_PUBLIC_API_WS_URL ?? "ws://localhost:8000";

async function connect(path: string): Promise<WebSocket | null> {
  // A short-lived single-use ticket is fetched over the normal, cookie-
  // authenticated /api/* path first, then spent immediately as a query
  // param on the direct-to-API connection, since the browser WebSocket API
  // has no way to attach an Authorization header to the handshake. Returns
  // null if the ticket couldn't be obtained (e.g. not logged in) -- the
  // caller decides what that means for its own UI.
  const ticketRes = await apiFetch("/api/auth/ws-ticket", { method: "POST" });
  if (!ticketRes.ok) return null;

  const { ticket } = (await ticketRes.json()) as { ticket: string };
  return new WebSocket(`${WS_BASE_URL}${path}?ticket=${encodeURIComponent(ticket)}`);
}

/** One project's board room -- ticket-move/assign/etc. events for whoever
 * has that specific project open (see apps/api/app/api/ws.py). */
export async function connectProjectSocket(projectId: string): Promise<WebSocket | null> {
  return connect(`/ws/projects/${projectId}`);
}

/** One connection per signed-in user, not per page -- a notification has
 * to reach someone regardless of which project (if any) they're currently
 * looking at, so unlike the project room above this can't be keyed on
 * anything page-specific. Meant to be opened once at the shell layout
 * level and held open across every authenticated page, not per-project-page
 * the way connectProjectSocket is. */
export async function connectNotificationSocket(): Promise<WebSocket | null> {
  return connect("/ws/notifications");
}
