import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import "./globals.css";
import { AuthProvider } from "../lib/auth-context";
import { ThemeProvider } from "../lib/theme-context";

// Runs before React hydrates so the very first paint already has the
// right theme — without this, the page would flash light-then-dark (or
// vice versa) as soon as ThemeProvider reads localStorage. Kept as a
// plain inline script, not a library: the whole logic is "read one key,
// default to dark," not worth a dependency.
const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem('wrkbase-theme');document.documentElement.setAttribute('data-theme', t==='light'?'light':'dark');}catch(e){document.documentElement.setAttribute('data-theme','dark');}})();`;

// IBM Plex over Inter/Geist: drawn by IBM for technical/engineering
// systems, not picked as a safe default. Sans for UI, Mono for anything
// that's a value — ticket ids, timestamps, status codes — see
// app/(shell)/projects/[projectId]/page.tsx for where that split matters.
const plexSans = IBM_Plex_Sans({
  variable: "--font-plex-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

export const metadata: Metadata = {
  title: "Wrkbase",
  description: "AI-native, security-aware project management",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${plexSans.variable} ${plexMono.variable} h-full antialiased`}
      // The inline script below sets data-theme on this element before
      // React hydrates, on purpose (see THEME_INIT_SCRIPT) — that makes
      // the server-rendered HTML (no data-theme yet) and the real DOM at
      // hydration time (data-theme already set) genuinely,
      // intentionally different. suppressHydrationWarning is the correct
      // escape hatch for exactly this pattern, not a way to paper over an
      // actual bug — the attribute itself is still applied correctly on
      // both server and client, just at different times.
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="min-h-full flex flex-col font-sans">
        <ThemeProvider>
          <AuthProvider>{children}</AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
