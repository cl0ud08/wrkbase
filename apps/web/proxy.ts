import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Deliberately cheap: only checks whether a session *might* exist (the
// refresh_token cookie is present), never validates the JWT or talks to the
// API. Real validation happens where it belongs — the backend, on every
// request — and expired-access-token recovery happens client-side via
// apiFetch's silent refresh (lib/api.ts). Doing that here would mean an
// extra network round trip for every single navigation.
// forgot-password/reset-password are public rather than logged-out-only:
// unlike /login and /signup, there's a real (if unusual) case for reaching
// them while a session is active elsewhere — e.g. clicking an old reset
// email on a second device that's still logged in — and redirecting that
// away would just break the confirm-token flow for no real security gain.
// /verify-email is public for the same reason: it's routinely opened
// from an email client that may not share a session with the browser
// that signed up, and verification isn't gated on being logged in anyway
// (see User.is_verified's docstring — soft-nudge, not a gate).
const ALWAYS_PUBLIC_PATHS = ["/", "/forgot-password", "/reset-password", "/verify-email"];
const LOGGED_OUT_ONLY_PATHS = ["/login", "/signup"];

// Next.js 16 renamed the `middleware.ts` file convention to `proxy.ts`
// (same NextRequest/NextResponse API, function just renamed) — this project
// pins a version new enough that the old name is deprecated.
export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const hasSession = request.cookies.has("refresh_token");

  if (LOGGED_OUT_ONLY_PATHS.includes(pathname)) {
    if (hasSession) {
      return NextResponse.redirect(new URL("/", request.url));
    }
    return NextResponse.next();
  }

  if (ALWAYS_PUBLIC_PATHS.includes(pathname)) {
    return NextResponse.next();
  }

  if (!hasSession) {
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("from", pathname);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  // /api is excluded deliberately: /api/auth/login and /api/auth/signup
  // themselves must stay reachable for logged-out visitors, and this
  // proxy's redirects don't make sense for JSON API responses anyway.
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};
