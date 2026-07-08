"use client";

import { usePathname, useRouter } from "next/navigation";

import { useAuth } from "../../lib/auth-context";
import ThemeToggle from "../../components/ThemeToggle";
import VerificationBanner from "../../components/VerificationBanner";

// First letter of up to two words, uppercased — a compact stand-in for an
// org "logo" until orgs have real ones. Deliberately separate from
// ticketPrefix (also derived from the org name, but a longer, stable
// identifier baked into every ticket key) — this is just a badge.
function orgInitials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

function NavLink({ href, label, active }: { href: string; label: string; active: boolean }) {
  return (
    <a
      href={href}
      className={`rounded-md px-2.5 py-1.5 text-sm transition-colors duration-100 ${
        active
          ? "bg-hover text-ink"
          : "text-ink-secondary hover:bg-hover hover:text-ink"
      }`}
    >
      {label}
    </a>
  );
}

export default function ShellLayout({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuth();
  const pathname = usePathname();
  const router = useRouter();

  async function handleLogout() {
    await logout();
    router.push("/login");
  }

  return (
    <div className="bg-grid flex min-h-full flex-1 flex-col bg-base">
      <header className="sticky top-0 z-20 flex h-12 flex-shrink-0 items-center justify-between gap-4 border-b border-line-subtle bg-surface px-4">
        <div className="flex min-w-0 items-center gap-4">
          <a href="/dashboard" className="flex flex-shrink-0 items-center gap-1.5 text-sm font-bold tracking-tight text-ink">
            <span className="h-1.5 w-1.5 rounded-sm bg-accent shadow-[0_0_8px_var(--accent)]" aria-hidden="true" />
            Wrkbase
          </a>

          {/* The signature element: always visible, ties the UI back to
              the thing Jira has no equivalent of — this org's data is
              actually tenant-isolated, not just filtered client-side. */}
          {user && (
            <div className="flex min-w-0 items-center gap-1.5 rounded-md border border-line bg-surface-2 py-0.5 pr-2.5 pl-0.5 text-xs text-ink-secondary">
              <span className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-[4px] bg-accent-subtle font-mono text-[10px] font-semibold text-accent-subtle-text">
                {orgInitials(user.orgName)}
              </span>
              <span className="hidden font-mono text-[9px] tracking-wider text-ink-tertiary uppercase sm:inline">
                org
              </span>
              <span className="truncate font-medium text-ink">{user.orgName}</span>
            </div>
          )}

          <nav className="hidden items-center gap-1 md:flex">
            <NavLink href="/dashboard" label="Projects" active={pathname === "/dashboard"} />
            <NavLink href="/team" label="Team" active={pathname === "/team"} />
          </nav>
        </div>

        {user && (
          <div className="flex flex-shrink-0 items-center gap-2">
            <span
              className="hidden max-w-[160px] truncate text-xs text-ink-secondary sm:inline"
              title={user.email}
            >
              {user.email}
            </span>
            <ThemeToggle compact />
            <button
              onClick={handleLogout}
              className="rounded-md border border-line px-2.5 py-1 text-xs font-medium text-ink-secondary transition-colors duration-100 hover:border-line-strong hover:text-ink"
            >
              Log out
            </button>
          </div>
        )}
      </header>

      {user && !user.isVerified && <VerificationBanner email={user.email} />}

      {/* Nav collapses off the top bar under md; surfaced here instead so
          it's still one tap away rather than hidden in an unbuilt menu. */}
      <nav className="flex items-center gap-1 border-b border-line-subtle bg-surface px-4 py-1.5 md:hidden">
        <NavLink href="/dashboard" label="Projects" active={pathname === "/dashboard"} />
        <NavLink href="/team" label="Team" active={pathname === "/team"} />
      </nav>

      <main className="flex flex-1 flex-col">{children}</main>
    </div>
  );
}
