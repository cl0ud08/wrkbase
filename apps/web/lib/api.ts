// Every call goes through the same-origin /api/* rewrite proxy (see
// next.config.ts), never the API's own port directly — that's what keeps
// the httpOnly auth cookies scoped to this origin.

let refreshInFlight: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  // De-duplicated: refresh tokens are single-use (they rotate on every
  // call — see the auth slice). If several requests fired in parallel each
  // independently called /api/auth/refresh after getting a 401, only the
  // first would succeed; the rest would race for an already-rotated-out
  // token and fail, incorrectly logging the user out. Sharing one in-flight
  // promise means concurrent callers all await the same single attempt.
  if (!refreshInFlight) {
    refreshInFlight = fetch("/api/auth/refresh", {
      method: "POST",
      credentials: "include",
    })
      .then((res) => res.ok)
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

/**
 * fetch() wrapper that transparently retries once via silent refresh on a
 * 401 — the frontend half of rotation (item 6). A second 401 (refresh
 * itself failed, e.g. the refresh token also expired or was revoked) is
 * returned as-is for the caller to treat as "actually logged out".
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(path, { ...init, credentials: "include" });
  if (res.status !== 401) return res;

  const refreshed = await refreshAccessToken();
  if (!refreshed) return res;

  return fetch(path, { ...init, credentials: "include" });
}
