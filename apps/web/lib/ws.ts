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

/**
 * Opens an authenticated WebSocket to a project's room. No pub/sub or
 * message handling yet (Phase 2 slice 1) -- this only proves the connection
 * itself: a short-lived single-use ticket is fetched over the normal,
 * cookie-authenticated /api/* path first, then spent immediately as a query
 * param on the direct-to-API connection, since the browser WebSocket API
 * has no way to attach an Authorization header to the handshake.
 *
 * Returns null if the ticket couldn't be obtained (e.g. not logged in) --
 * the caller decides what that means for its own UI.
 */
export async function connectProjectSocket(projectId: string): Promise<WebSocket | null> {
  const ticketRes = await apiFetch("/api/auth/ws-ticket", { method: "POST" });
  if (!ticketRes.ok) return null;

  const { ticket } = (await ticketRes.json()) as { ticket: string };
  const url = `${WS_BASE_URL}/ws/projects/${projectId}?ticket=${encodeURIComponent(ticket)}`;
  return new WebSocket(url);
}
