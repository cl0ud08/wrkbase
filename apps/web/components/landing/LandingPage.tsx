"use client";

import ThemeToggle from "../ThemeToggle";

const REPO_URL = "https://github.com/cl0ud08/wrkbase";

const FEATURES: { tag: string; title: string; body: string }[] = [
  {
    tag: "RLS",
    title: "Tenant isolation at the database",
    body: "Row-Level Security is forced on every table — org data is walled off in Postgres itself, not filtered in application code. A missed WHERE clause can't leak another tenant's rows.",
  },
  {
    tag: "BOARD",
    title: "A board that doesn't fight you",
    body: "Drag-and-drop Kanban with epics, stories, tasks, and subtasks. Fractional positioning means a drop never renumbers the rest of the column.",
  },
  {
    tag: "IDS",
    title: "Real ticket keys",
    body: "Every ticket gets a stable, per-org sequential ID like WRK-142 — not a truncated UUID standing in for one.",
  },
  {
    tag: "TEAM",
    title: "Invites that expire, roles that stick",
    body: "Admin, member, and viewer roles with single-use invite tokens. Remove someone and their session dies immediately — no lingering access.",
  },
  {
    tag: "DATA",
    title: "Nothing is really gone",
    body: "Deleting a ticket or project is a soft-delete with a restore endpoint behind it — recoverable by design, not a silent DELETE FROM.",
  },
  {
    tag: "CI",
    title: "A pipeline that actually checks",
    body: "pip-audit, npm audit, and gitleaks run on every push. A known CVE or a leaked secret fails the build — it doesn't wait for someone to notice.",
  },
];

const SECURITY_LOG: string[] = [
  "FORCE ROW LEVEL SECURITY — enabled on every tenant-scoped table, no bypass even for the table owner",
  "composite foreign keys — every cross-table reference carries (id, org_id), so RLS can't be routed around by a bad join",
  "argon2id — password hashing, tuned for this workload rather than left at library defaults",
  "opaque, revocable tokens — refresh + invite tokens are DB-backed, not JWTs; redemption always hits the database anyway, so statelessness buys nothing but unrevocability",
  "two Postgres roles — a superuser for migrations, a least-privilege runtime role for everything the API actually does",
];

const STACK = [
  { name: "Next.js", note: "App Router, TypeScript" },
  { name: "FastAPI", note: "async, typed end to end" },
  { name: "PostgreSQL", note: "+ pgvector, provisioned for what's next" },
  { name: "Redis", note: "session + rate-limit state" },
  { name: "Docker Compose", note: "one command, full stack" },
];

function FeatureCard({ tag, title, body }: { tag: string; title: string; body: string }) {
  return (
    <div className="flex flex-col gap-2.5 rounded-lg border border-line-subtle bg-surface p-5 transition-colors duration-150 hover:border-line">
      <span className="w-fit rounded-sm bg-accent-subtle px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wider text-accent-subtle-text uppercase">
        {tag}
      </span>
      <h3 className="text-[15px] font-semibold text-ink">{title}</h3>
      <p className="text-sm leading-relaxed text-ink-secondary">{body}</p>
    </div>
  );
}

function MockTypeBadge({
  label,
  text,
  bg,
}: {
  label: string;
  text: string;
  bg: string;
}) {
  return (
    <span
      className={`rounded-sm px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wide uppercase ${text} ${bg}`}
    >
      {label}
    </span>
  );
}

function HeroMock() {
  return (
    <div className="w-full max-w-[420px] rounded-lg border border-line bg-surface shadow-[var(--shadow-elevated)]">
      <div className="flex items-center justify-between gap-3 border-b border-line-subtle px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-sm bg-accent shadow-[0_0_8px_var(--accent)]" aria-hidden="true" />
          <span className="text-sm font-bold tracking-tight text-ink">Wrkbase</span>
        </div>
        <div className="flex items-center gap-1.5 rounded-md border border-line bg-surface-2 py-0.5 pr-2 pl-0.5 text-xs text-ink-secondary">
          <span className="flex h-5 w-5 items-center justify-center rounded-[4px] bg-accent-subtle font-mono text-[10px] font-semibold text-accent-subtle-text">
            AC
          </span>
          <span className="hidden font-mono text-[9px] tracking-wider text-ink-tertiary uppercase sm:inline">
            org
          </span>
          <span className="font-medium text-ink">Acme Corp</span>
        </div>
        <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-[4px] border border-line bg-hover font-mono text-[10px] font-semibold text-ink-secondary">
          JD
        </span>
      </div>

      <div className="flex items-center justify-between gap-2 px-4 pt-3.5 pb-2.5">
        <div>
          <p className="text-sm font-semibold text-ink">Payments Platform</p>
          <p className="text-xs text-ink-tertiary">12 open · 3 in review</p>
        </div>
        <span className="rounded-md bg-accent px-2.5 py-1 text-xs font-semibold text-accent-on">
          New ticket
        </span>
      </div>

      <div className="flex flex-col gap-2 px-4 pb-4">
        <div className="rounded-md border border-line-subtle bg-surface-2 p-2.5 shadow-[var(--shadow-card)]">
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <span className="font-mono text-[11px] text-ink-tertiary">WRK-142</span>
            <MockTypeBadge label="Task" text="text-type-task" bg="bg-type-task-bg" />
          </div>
          <p className="mb-2 text-sm leading-snug text-ink">Rate-limit the invite endpoint</p>
          <span className="flex h-5 w-5 items-center justify-center rounded-[4px] border border-line bg-hover font-mono text-[9px] font-semibold text-ink-secondary">
            JD
          </span>
        </div>

        <div className="rounded-md border border-line-subtle bg-surface-2 p-2.5 shadow-[var(--shadow-card)]">
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <span className="font-mono text-[11px] text-ink-tertiary">WRK-137</span>
            <MockTypeBadge label="Epic" text="text-type-epic" bg="bg-type-epic-bg" />
          </div>
          <p className="mb-2 text-sm leading-snug text-ink">Support CSV export for invoices</p>
          <span className="flex h-5 w-5 items-center justify-center rounded-[4px] border border-dashed border-line text-ink-tertiary" />
        </div>
      </div>
    </div>
  );
}

export default function LandingPage() {
  return (
    <div className="bg-grid flex flex-col bg-base">
      <nav className="sticky top-0 z-20 border-b border-line-subtle bg-base/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-3.5">
          <div className="flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 rounded-sm bg-accent shadow-[0_0_8px_var(--accent)]" aria-hidden="true" />
            <span className="text-sm font-bold tracking-tight text-ink">Wrkbase</span>
          </div>
          <div className="flex items-center gap-2">
            <a
              href={REPO_URL}
              className="hidden rounded-md px-3 py-1.5 text-sm text-ink-secondary transition-colors duration-100 hover:text-ink sm:inline-block"
            >
              Source
            </a>
            <a
              href="/login"
              className="rounded-md px-3 py-1.5 text-sm text-ink-secondary transition-colors duration-100 hover:text-ink"
            >
              Sign in
            </a>
            <a
              href="/signup"
              className="rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
            >
              Get started
            </a>
          </div>
        </div>
      </nav>

      <section className="relative overflow-hidden border-b border-line-subtle">
        <div
          className="pointer-events-none absolute inset-0"
          style={{
            background:
              "radial-gradient(ellipse 55% 45% at 22% 15%, var(--accent-subtle) 0%, transparent 65%)",
            opacity: 0.6,
          }}
          aria-hidden="true"
        />
        <div className="relative mx-auto flex max-w-6xl flex-col items-center gap-14 px-6 py-20 lg:flex-row lg:items-center lg:py-28">
          <div className="flex max-w-xl flex-col items-start gap-5">
            <span className="rounded-sm border border-line bg-surface px-2 py-1 font-mono text-[11px] tracking-wider text-ink-tertiary uppercase">
              Self-hosted · security-aware project management
            </span>
            <h1 className="text-4xl leading-[1.1] font-bold tracking-tight text-ink text-balance sm:text-5xl">
              Built like a console, <span className="text-accent">not a form.</span>
            </h1>
            <p className="text-base leading-relaxed text-ink-secondary">
              Wrkbase is a Jira-shaped tool rebuilt around a simple bet: tenant isolation
              belongs in the database, not scattered across application code. Kanban boards,
              hierarchical tickets, and team management — for teams that read the audit log.
            </p>
            <div className="flex items-center gap-3">
              <a
                href="/signup"
                className="rounded-md bg-accent px-4 py-2.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
              >
                Get started
              </a>
              <a
                href={REPO_URL}
                className="rounded-md border border-line px-4 py-2.5 text-sm font-medium text-ink transition-colors duration-100 hover:border-line-strong hover:bg-hover"
              >
                View source on GitHub
              </a>
              <ThemeToggle />
            </div>
          </div>

          <div className="flex w-full justify-center lg:justify-end">
            <HeroMock />
          </div>
        </div>
      </section>

      <section className="mx-auto w-full max-w-6xl px-6 py-20">
        <div className="mb-10 flex flex-col gap-2">
          <span className="font-mono text-xs tracking-wider text-accent uppercase">What&apos;s actually built</span>
          <h2 className="text-2xl font-bold tracking-tight text-ink">
            Not a mockup — every card below is a real endpoint.
          </h2>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((f) => (
            <FeatureCard key={f.tag} {...f} />
          ))}
        </div>
      </section>

      <section className="border-y border-line-subtle bg-surface">
        <div className="mx-auto grid max-w-6xl grid-cols-1 gap-10 px-6 py-20 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
          <div className="flex flex-col gap-3">
            <span className="font-mono text-xs tracking-wider text-accent uppercase">Security model</span>
            <h2 className="text-2xl font-bold tracking-tight text-ink text-balance">
              The pitch is &quot;security-aware.&quot; Here&apos;s what backs it.
            </h2>
            <p className="text-sm leading-relaxed text-ink-secondary">
              Every mechanism below is enforced by Postgres or the auth layer directly —
              nothing here is a comment saying &quot;remember to check this.&quot;
            </p>
          </div>
          <div className="rounded-lg border border-line-subtle bg-base p-5 shadow-[var(--shadow-card)]">
            <div className="mb-3 flex items-center gap-1.5 border-b border-line-subtle pb-3">
              <span className="h-2 w-2 rounded-full bg-danger" aria-hidden="true" />
              <span className="h-2 w-2 rounded-full bg-warning" aria-hidden="true" />
              <span className="h-2 w-2 rounded-full bg-success" aria-hidden="true" />
              <span className="ml-2 font-mono text-[11px] text-ink-tertiary">security.log</span>
            </div>
            <ul className="flex flex-col gap-2.5 font-mono text-[12.5px] leading-relaxed">
              {SECURITY_LOG.map((line, i) => (
                <li key={i} className="flex gap-2.5 text-ink-secondary">
                  <span className="text-success">✓</span>
                  <span>{line}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      <section className="mx-auto w-full max-w-6xl px-6 py-20">
        <div className="mb-8 flex flex-col gap-2">
          <span className="font-mono text-xs tracking-wider text-accent uppercase">Under the hood</span>
          <h2 className="text-2xl font-bold tracking-tight text-ink">One Docker Compose stack, no managed services.</h2>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {STACK.map((s) => (
            <div
              key={s.name}
              className="flex flex-col gap-1 rounded-lg border border-line-subtle bg-surface p-4"
            >
              <span className="text-sm font-semibold text-ink">{s.name}</span>
              <span className="text-xs leading-snug text-ink-tertiary">{s.note}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="border-t border-line-subtle">
        <div className="mx-auto flex max-w-6xl flex-col items-center gap-5 px-6 py-20 text-center">
          <h2 className="text-2xl font-bold tracking-tight text-ink">
            Spin it up and see the isolation for yourself.
          </h2>
          <p className="max-w-md text-sm text-ink-secondary">
            Create an org, invite a teammate, and try to find a way to see another
            tenant&apos;s data. There isn&apos;t one.
          </p>
          <a
            href="/signup"
            className="rounded-md bg-accent px-5 py-2.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
          >
            Create your org
          </a>
        </div>
      </section>

      <footer className="border-t border-line-subtle">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-3 px-6 py-8 text-xs text-ink-tertiary sm:flex-row">
          <span>Built solo. Open source.</span>
          <a href={REPO_URL} className="font-mono transition-colors duration-100 hover:text-ink-secondary">
            github.com/cl0ud08/wrkbase
          </a>
        </div>
      </footer>
    </div>
  );
}
