import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Same-origin proxy to the API. This is what lets the API's httpOnly
  // auth cookies land on the frontend's own origin: the browser only ever
  // talks to itself (this Next.js server), so a Set-Cookie in the proxied
  // response is scoped to this origin, not the API's — which is what makes
  // both the httpOnly cookie flow (item 3) and Next.js middleware reading
  // that cookie directly (item 5) possible at all. Without this, the API's
  // cookies would be scoped to the API's own origin/port and Next.js
  // middleware could never see them.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.API_INTERNAL_URL}/:path*`,
      },
    ];
  },
};

export default nextConfig;
