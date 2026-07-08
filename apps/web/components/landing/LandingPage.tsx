"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AnimatePresence,
  animate,
  motion,
  useInView,
  useMotionValue,
  useReducedMotion,
  useTransform,
  type Variants,
} from "motion/react";

import { useAuth } from "../../lib/auth-context";
import ThemeToggle from "../ThemeToggle";

// ---------------------------------------------------------------------
// Content. Accent is reserved for two things only, throughout this
// page: primary CTAs, and the AI feature specifically — everything
// else reads through the muted neutral/semantic tokens.
// ---------------------------------------------------------------------

const FEATURES: { title: string; body: string; isAI?: boolean; badge?: string }[] = [
  {
    title: "Break work down the way it actually happens",
    body: "Epics split into stories, stories into tasks, tasks into subtasks — a real hierarchy, not a flat list with a label bolted on. See the whole tree, or drill into exactly the slice your team owns.",
  },
  {
    title: "A board your team will actually use",
    body: "Configure the columns to match how your team works, not the other way around. Drag a ticket to the next column and it's there instantly — no reload, no lag waiting for the click to register.",
  },
  {
    title: "Plan sprints without the spreadsheet",
    body: "Pull tickets from the backlog into a sprint and estimate with story points — the sprint's total commitment updates as you go. Close it out and unfinished work returns to the backlog on its own, so nothing quietly falls through.",
  },
  {
    title: "Bring your whole team in, with the right access",
    body: "Invite teammates by email and assign admin, member, or viewer roles. Hand a ticket off with a click. Every invite is single-use and expires on its own — no stale links floating around months later.",
  },
  {
    title: "Your team's data, walled off by architecture — not just policy",
    body: "Every organization's data is isolated at the infrastructure level, not filtered by application code a future bug could bypass. It's the same standard of isolation regulated industries build to — in from day one, not bolted on after an incident.",
  },
  {
    title: "Let AI take the first pass",
    body: "Describe a ticket in plain language and AI fills in the structure. AI-assisted triage suggests labels and an assignee on new tickets, and flags security-sensitive changes — auth, payments, permissions — for review before they ship, not after.",
    isAI: true,
    badge: "Early access",
  },
];

type PlanTier = {
  name: string;
  price: string;
  cadence: string;
  description: string;
  cta: string;
  features: string[];
  highlighted?: boolean;
};

const PLANS: PlanTier[] = [
  {
    name: "Free",
    price: "$0",
    cadence: "forever",
    description: "For a small team getting real work organized.",
    cta: "Get started",
    features: [
      "Up to 5 team members",
      "Unlimited projects and tickets",
      "Kanban board with configurable workflows",
      "Epic → story → task → subtask hierarchy",
      "Community support",
    ],
  },
  {
    name: "Team",
    price: "$5",
    cadence: "per user / month",
    description: "For teams ready to plan sprints, not just track tickets.",
    cta: "Start free trial",
    highlighted: true,
    features: [
      "Everything in Free",
      "Unlimited team members",
      "Sprints, backlog planning, story points",
      "AI-assisted ticket triage",
      "Natural language ticket creation",
      "Priority email support",
    ],
  },
  {
    name: "Business",
    price: "$12",
    cadence: "per user / month",
    description: "For teams that want AI across the whole workflow.",
    cta: "Start free trial",
    features: [
      "Everything in Team",
      "Security-review flagging on sensitive changes",
      "At-risk ticket detection",
      "AI sprint summaries",
      "Full audit log access",
      "Priority support with faster response times",
    ],
  },
];

const AUDIENCE: { title: string; body: string }[] = [
  {
    title: "Teams who've had the security conversation",
    body: "Whether it was a near-miss, an audit, or just a founder who reads the news — once you've had to explain how your data is isolated, you don't want to explain it twice.",
  },
  {
    title: "Teams that outgrew a flat ticket list",
    body: "When \"just add a label\" stopped being enough to represent how work actually breaks down, and a real epic-to-subtask hierarchy became the thing you were missing.",
  },
  {
    title: "Teams who want AI to remove work, not add a tool",
    body: "AI that drafts the ticket and flags the risky change belongs inside the workflow you already have — not in a separate tab you have to remember to check.",
  },
];

const DEMO_TEXT = "bug: login fails on Safari, high priority";
const DEMO_RESULT = {
  key: "WRK-201",
  title: "Login fails on Safari",
  priority: "High",
  label: "Bug",
};

const LIFECYCLE_STAGES = ["Created", "AI-triaged", "On the board", "Shipped"] as const;

// ---------------------------------------------------------------------
// Hero demo: type -> process -> assemble. The single most orchestrated
// moment on the page. Auto-plays once (after the hero's own entrance
// settles), replayable via a button. Reduced motion skips the whole
// state machine and renders the finished input + card together,
// statically, with no transitions at all.
// ---------------------------------------------------------------------

type DemoPhase = "idle" | "typing" | "processing" | "revealed";

const cardContainer: Variants = {
  hidden: { opacity: 0, scale: 0.96 },
  visible: {
    opacity: 1,
    scale: 1,
    transition: { duration: 0.15, ease: "easeOut", staggerChildren: 0.12, delayChildren: 0.1 },
  },
};
const cardChild: Variants = {
  hidden: { opacity: 0, y: 6, scale: 0.94 },
  visible: { opacity: 1, y: 0, scale: 1, transition: { duration: 0.18, ease: "easeOut" } },
};

function HeroDemo() {
  const reduceMotion = useReducedMotion();
  const [phase, setPhase] = useState<DemoPhase>(reduceMotion ? "revealed" : "idle");
  const [typed, setTyped] = useState(reduceMotion ? DEMO_TEXT : "");
  const hasAutoPlayed = useRef(false);

  const play = useCallback(() => {
    if (reduceMotion) return;
    setPhase("typing");
    setTyped("");
  }, [reduceMotion]);

  // Typing beat: ~45ms/char, deliberately brisk rather than naturalistic
  // — it should read as fast, not as a realistic person typing.
  useEffect(() => {
    if (phase !== "typing") return;
    let i = 0;
    const id = setInterval(() => {
      i += 1;
      setTyped(DEMO_TEXT.slice(0, i));
      if (i >= DEMO_TEXT.length) {
        clearInterval(id);
        setTimeout(() => setPhase("processing"), 250);
      }
    }, 45);
    return () => clearInterval(id);
  }, [phase]);

  // Processing beat: short on purpose — a signal, not a spinner to wait out.
  useEffect(() => {
    if (phase !== "processing") return;
    const t = setTimeout(() => setPhase("revealed"), 300);
    return () => clearTimeout(t);
  }, [phase]);

  useEffect(() => {
    if (reduceMotion || hasAutoPlayed.current) return;
    hasAutoPlayed.current = true;
    const t = setTimeout(play, 800);
    return () => clearTimeout(t);
  }, [reduceMotion, play]);

  const canReplay = phase === "revealed" || phase === "idle";

  return (
    <div className="mx-auto flex w-full max-w-md flex-col items-center gap-3">
      <span className="font-mono text-[11px] tracking-wider text-accent uppercase">Try it</span>

      <div
        className={`flex w-full items-center gap-2 rounded-md border bg-surface px-3.5 py-2.5 shadow-[var(--shadow-card)] transition-colors duration-200 ${
          phase === "processing" ? "border-accent" : "border-line"
        }`}
      >
        <span className="flex-1 truncate text-left text-sm text-ink">
          {typed || <span className="text-ink-tertiary">Describe a ticket in plain English…</span>}
        </span>
        {!reduceMotion && (
          <AnimatePresence mode="wait">
            {(phase === "idle" || phase === "typing") && (
              <motion.span
                key="cursor"
                className="h-4 w-[2px] flex-shrink-0 bg-ink-tertiary"
                animate={{ opacity: [1, 1, 0, 0] }}
                exit={{ opacity: 0, transition: { duration: 0.12 } }}
                transition={{ duration: 0.9, repeat: Infinity, times: [0, 0.5, 0.51, 1] }}
              />
            )}
            {phase === "processing" && (
              <motion.span
                key="dot"
                className="h-2 w-2 flex-shrink-0 rounded-full bg-accent"
                animate={{ opacity: [1, 0.35, 1] }}
                exit={{ opacity: 0, transition: { duration: 0.15 } }}
                transition={{ duration: 0.5, repeat: Infinity }}
              />
            )}
          </AnimatePresence>
        )}
      </div>

      {!reduceMotion && (
        <button
          onClick={play}
          disabled={!canReplay}
          className="font-mono text-xs text-ink-tertiary underline decoration-dotted transition-colors duration-150 hover:text-ink disabled:pointer-events-none disabled:opacity-40"
        >
          {phase === "revealed" ? "Replay ↻" : "Watch it work"}
        </button>
      )}

      <AnimatePresence>
        {phase === "revealed" && (
          <motion.div
            variants={reduceMotion ? undefined : cardContainer}
            initial={reduceMotion ? undefined : "hidden"}
            animate={reduceMotion ? undefined : "visible"}
            exit={reduceMotion ? undefined : { opacity: 0 }}
            className="w-full rounded-md border border-line-subtle bg-surface-2 p-3 shadow-[var(--shadow-card)]"
          >
            <motion.div variants={reduceMotion ? undefined : cardChild} className="mb-1.5 flex items-center justify-between gap-2">
              <span className="font-mono text-[11px] text-ink-tertiary">{DEMO_RESULT.key}</span>
              <span className="rounded-sm bg-danger-bg px-1.5 py-0.5 font-mono text-[9px] font-semibold tracking-wide text-danger uppercase">
                {DEMO_RESULT.label}
              </span>
            </motion.div>
            <motion.p variants={reduceMotion ? undefined : cardChild} className="mb-2.5 text-sm leading-snug text-ink">
              {DEMO_RESULT.title}
            </motion.p>
            <motion.div variants={reduceMotion ? undefined : cardChild} className="flex items-center justify-between gap-2">
              <span className="rounded-sm bg-warning-bg px-1.5 py-0.5 font-mono text-[9px] font-semibold tracking-wide text-warning uppercase">
                {DEMO_RESULT.priority} priority
              </span>
              <span
                className="relative flex h-5 w-5 items-center justify-center rounded-[4px] border border-line bg-hover font-mono text-[9px] font-semibold text-ink-secondary"
                title="Auto-assigned by AI"
              >
                JD
                <span className="absolute -top-1 -right-1 h-2 w-2 rounded-full bg-accent shadow-[0_0_4px_var(--accent)]" aria-hidden="true" />
              </span>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ---------------------------------------------------------------------
// Feature grid + the small ambient Kanban loop illustrating feature 2.
// ---------------------------------------------------------------------

function KanbanLoop() {
  const reduceMotion = useReducedMotion();
  const [moved, setMoved] = useState(false);

  useEffect(() => {
    if (reduceMotion) return;
    const id = setInterval(() => setMoved((m) => !m), 3400);
    return () => clearInterval(id);
  }, [reduceMotion]);

  if (reduceMotion) {
    return (
      <div className="mt-2 flex items-center gap-2 rounded-md border border-line-subtle bg-surface-2 p-2.5">
        <span className="rounded-sm bg-info-bg px-1.5 py-0.5 font-mono text-[9px] font-semibold text-info uppercase">In Progress</span>
        <span className="text-ink-tertiary" aria-hidden="true">→</span>
        <span className="rounded-sm bg-success-bg px-1.5 py-0.5 font-mono text-[9px] font-semibold text-success uppercase">Done</span>
      </div>
    );
  }

  return (
    <div className="relative mt-2 flex gap-2 rounded-md border border-line-subtle bg-surface-2 p-2.5">
      <div className="flex-1 rounded bg-base/60 px-2 py-1">
        <span className="font-mono text-[9px] font-semibold tracking-wider text-ink-tertiary uppercase">In Progress</span>
      </div>
      <div className="flex-1 rounded bg-base/60 px-2 py-1">
        <span className="font-mono text-[9px] font-semibold tracking-wider text-ink-tertiary uppercase">Done</span>
      </div>
      <motion.div
        className="absolute top-8 left-2.5 w-[calc(50%-1.25rem)] rounded-sm border border-line-subtle bg-surface px-2 py-1.5 shadow-[var(--shadow-card)]"
        animate={{
          x: moved ? "calc(100% + 0.5rem)" : 0,
          y: [0, -3, -3, 0],
          boxShadow: moved
            ? "var(--shadow-card)"
            : "var(--shadow-card)",
        }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
      >
        <span className="font-mono text-[9px] text-ink-tertiary">WRK-84</span>
      </motion.div>
    </div>
  );
}

function FeatureBlock({ title, body, isAI, badge }: (typeof FEATURES)[number]) {
  return (
    <div className="flex h-full flex-col gap-3 rounded-lg border border-line-subtle bg-surface p-6 transition-all duration-200 hover:-translate-y-1 hover:border-line hover:shadow-[var(--shadow-card-hover)]">
      {badge && (
        <span
          className={`w-fit rounded-sm px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wider uppercase ${
            isAI ? "bg-accent-subtle text-accent-subtle-text" : "bg-neutral-bg text-neutral"
          }`}
        >
          {badge}
        </span>
      )}
      <h3 className="text-lg font-semibold text-ink text-balance">{title}</h3>
      <p className="text-sm leading-relaxed text-ink-secondary">{body}</p>
      {title.startsWith("A board") && <KanbanLoop />}
    </div>
  );
}

// ---------------------------------------------------------------------
// Before/after + animated time stat
// ---------------------------------------------------------------------

function CountUpNumber({ target, run, suffix = "" }: { target: number; run: boolean; suffix?: string }) {
  const reduceMotion = useReducedMotion();
  const value = useMotionValue(reduceMotion ? target : 0);
  const rounded = useTransform(value, (v) => `${Math.round(v)}${suffix}`);

  useEffect(() => {
    if (!run) return;
    if (reduceMotion) {
      value.set(target);
      return;
    }
    const controls = animate(value, target, { duration: 1.1, ease: [0.16, 1, 0.3, 1] });
    return () => controls.stop();
  }, [run, target, reduceMotion, value]);

  return <motion.span>{rounded}</motion.span>;
}

function BeforeAfter() {
  const ref = useRef<HTMLDivElement | null>(null);
  const inView = useInView(ref, { once: true, amount: 0.5 });

  return (
    <div className="border-t border-line-subtle pt-16">
      <div className="mx-auto mb-10 flex max-w-2xl flex-col items-center gap-2 text-center">
        <h3 className="text-2xl font-bold tracking-tight text-ink text-balance">One sentence vs. six fields.</h3>
      </div>
      <div className="mx-auto grid max-w-3xl grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-2.5 rounded-lg border border-line-subtle bg-surface-2 p-5">
          <span className="font-mono text-[10px] font-semibold tracking-wider text-ink-tertiary uppercase">The old way</span>
          <div className="flex flex-col gap-2">
            {["Title", "Description", "Type", "Priority", "Assignee"].map((f) => (
              <div key={f} className="rounded border border-line-subtle bg-base px-2.5 py-1.5 text-xs text-ink-tertiary">
                {f}
              </div>
            ))}
            <div className="mt-1 rounded border border-line px-2.5 py-1.5 text-center text-xs font-medium text-ink-secondary">
              Create
            </div>
          </div>
        </div>
        <div className="flex flex-col gap-2.5 rounded-lg border border-accent bg-surface p-5">
          <span className="font-mono text-[10px] font-semibold tracking-wider text-accent uppercase">With Wrkbase</span>
          <div className="rounded border border-line bg-base px-2.5 py-2 text-xs text-ink">
            bug: login fails on Safari, high priority
          </div>
          <div className="flex items-center justify-center py-1 text-ink-tertiary" aria-hidden="true">
            ↓
          </div>
          <div className="flex items-center justify-between rounded border border-line-subtle bg-surface-2 px-2.5 py-1.5">
            <span className="font-mono text-[10px] text-ink-tertiary">WRK-201</span>
            <span className="rounded-sm bg-danger-bg px-1.5 py-0.5 font-mono text-[9px] font-semibold text-danger uppercase">Bug</span>
          </div>
        </div>
      </div>

      <div ref={ref} className="mx-auto mt-10 flex max-w-md items-center justify-center gap-8 text-center">
        <div className="flex flex-col items-center gap-1">
          <span className="font-mono text-4xl font-bold tracking-tight text-ink-secondary tabular-nums">
            ~<CountUpNumber target={90} run={inView} />
          </span>
          <span className="text-xs text-ink-tertiary">Manual creation, seconds</span>
        </div>
        <span className="text-2xl text-ink-tertiary" aria-hidden="true">
          →
        </span>
        <div className="flex flex-col items-center gap-1">
          <span className="font-mono text-4xl font-bold tracking-tight text-accent tabular-nums">
            ~<CountUpNumber target={8} run={inView} />
          </span>
          <span className="text-xs text-ink-tertiary">With Wrkbase, seconds</span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------
// Ticket lifecycle scroll sequence — the second orchestrated moment.
// Each stage triggers independently as it scrolls into view.
// ---------------------------------------------------------------------

function LifecycleStage({ index, label }: { index: number; label: (typeof LIFECYCLE_STAGES)[number] }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const inView = useInView(ref, { once: true, amount: 0.6 });
  const reduceMotion = useReducedMotion();
  const active = reduceMotion || inView;

  return (
    <div ref={ref} className="flex flex-1 flex-col items-center gap-3">
      {index > 0 && (
        <div className="relative -mt-3 mb-1 hidden h-px w-full bg-line-subtle sm:block">
          <motion.div
            className="absolute inset-y-0 left-0 bg-accent"
            initial={reduceMotion ? false : { width: "0%" }}
            animate={{ width: active ? "100%" : "0%" }}
            transition={{ duration: 0.25, ease: "easeOut" }}
          />
        </div>
      )}
      <motion.div
        initial={reduceMotion ? false : { opacity: 0, y: 10, scale: 0.95 }}
        animate={active ? { opacity: 1, y: 0, scale: 1 } : {}}
        transition={{ duration: 0.3, ease: "easeOut", delay: index > 0 ? 0.2 : 0 }}
        className="flex w-full max-w-[180px] flex-col items-center gap-2 rounded-lg border border-line-subtle bg-surface p-4 text-center"
      >
        <span
          className={`h-2 w-2 rounded-full ${
            index === 0
              ? "bg-neutral"
              : index === LIFECYCLE_STAGES.length - 1
                ? "bg-success"
                : "bg-accent"
          }`}
          aria-hidden="true"
        />
        <span className="font-mono text-[10px] font-semibold tracking-wider text-ink-tertiary uppercase">{label}</span>
        {index === 1 && (
          <div className="flex gap-1">
            <span className="rounded-sm bg-warning-bg px-1 py-0.5 font-mono text-[8px] font-semibold text-warning uppercase">High</span>
            <span className="rounded-sm bg-danger-bg px-1 py-0.5 font-mono text-[8px] font-semibold text-danger uppercase">Bug</span>
          </div>
        )}
        {index >= 2 && (
          <div className="w-full rounded border border-line-subtle bg-surface-2 p-1.5 text-left">
            <span className="font-mono text-[9px] text-ink-tertiary">WRK-201</span>
            <p className="truncate text-[11px] text-ink">Login fails on Safari</p>
          </div>
        )}
      </motion.div>
    </div>
  );
}

function LifecycleSequence() {
  return (
    <div className="border-t border-line-subtle pt-16">
      <div className="mx-auto mb-12 flex max-w-2xl flex-col items-center gap-2 text-center">
        <span className="font-mono text-xs tracking-wider text-accent uppercase">How it moves</span>
        <h3 className="text-2xl font-bold tracking-tight text-ink text-balance">From sentence to shipped.</h3>
        <p className="text-sm text-ink-secondary">
          One ticket, start to finish — created in plain English, triaged automatically, and shipped
          without anyone updating a status by hand.
        </p>
      </div>
      <div className="mx-auto flex max-w-3xl flex-col items-stretch gap-6 sm:flex-row sm:items-start">
        {LIFECYCLE_STAGES.map((label, i) => (
          <LifecycleStage key={label} index={i} label={label} />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------
// Pricing — hover lift/glow only, no entrance motion.
// ---------------------------------------------------------------------

function PlanCard({ plan }: { plan: PlanTier }) {
  return (
    <motion.div
      whileHover={{ y: -4 }}
      transition={{ duration: 0.15, ease: "easeOut" }}
      className={`flex h-full flex-col gap-5 rounded-xl border p-6 transition-shadow duration-200 ${
        plan.highlighted
          ? "border-accent bg-surface shadow-[var(--shadow-elevated)] hover:shadow-[0_0_0_1px_var(--accent),var(--shadow-elevated)]"
          : "border-line-subtle bg-surface hover:border-line hover:shadow-[var(--shadow-card-hover)]"
      }`}
    >
      {plan.highlighted && (
        <span className="w-fit rounded-sm bg-accent px-2 py-0.5 font-mono text-[10px] font-semibold tracking-wider text-accent-on uppercase">
          Most popular
        </span>
      )}
      <div>
        <h3 className="text-base font-semibold text-ink">{plan.name}</h3>
        <p className="mt-1 text-sm text-ink-secondary">{plan.description}</p>
      </div>
      <div className="flex items-baseline gap-1.5">
        <span className="font-mono text-3xl font-bold tracking-tight text-ink">{plan.price}</span>
        <span className="text-xs text-ink-tertiary">{plan.cadence}</span>
      </div>
      <a
        href="/signup"
        className={`rounded-md px-4 py-2.5 text-center text-sm font-semibold transition-all duration-150 hover:-translate-y-0.5 ${
          plan.highlighted
            ? "bg-accent text-accent-on hover:bg-accent-hover hover:shadow-[var(--shadow-card-hover)]"
            : "border border-line text-ink hover:border-line-strong hover:bg-hover"
        }`}
      >
        {plan.cta}
      </a>
      <ul className="flex flex-col gap-2.5 border-t border-line-subtle pt-5 text-sm">
        {plan.features.map((f) => (
          <li key={f} className="flex items-start gap-2.5 text-ink-secondary">
            <span className="mt-0.5 text-success">✓</span>
            <span>{f}</span>
          </li>
        ))}
      </ul>
    </motion.div>
  );
}

// ---------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------

export default function LandingPage() {
  const { user, loading } = useAuth();
  const signedIn = !loading && !!user;
  const reduceMotion = useReducedMotion();

  // Always just the raf-deferred flip, no branch that sets state
  // synchronously in the effect body — reduced-motion users don't need a
  // separate path here: `entered` below is true for them immediately on
  // first paint regardless of `mounted`, and the global
  // prefers-reduced-motion rule already zeroes the transition duration,
  // so there's nothing left to defer.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setMounted(true));
    return () => cancelAnimationFrame(raf);
  }, []);
  const entered = mounted || !!reduceMotion;

  const primaryHref = signedIn ? "/dashboard" : "/signup";
  const primaryLabel = signedIn ? "Go to dashboard" : "Start free trial";

  return (
    <div className="bg-grid flex flex-col bg-base">
      <nav className="sticky top-0 z-20 border-b border-line-subtle bg-base/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-3.5">
          <div className="flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 rounded-sm bg-accent shadow-[0_0_8px_var(--accent)]" aria-hidden="true" />
            <span className="text-sm font-bold tracking-tight text-ink">Wrkbase</span>
          </div>
          <div className="hidden items-center gap-1 sm:flex">
            <a href="#features" className="rounded-md px-3 py-1.5 text-sm text-ink-secondary transition-colors duration-100 hover:text-ink">
              Features
            </a>
            <a href="#pricing" className="rounded-md px-3 py-1.5 text-sm text-ink-secondary transition-colors duration-100 hover:text-ink">
              Pricing
            </a>
          </div>
          <div className="flex items-center gap-2">
            <ThemeToggle compact />
            {!signedIn && (
              <a href="/login" className="rounded-md px-3 py-1.5 text-sm text-ink-secondary transition-colors duration-100 hover:text-ink">
                Sign in
              </a>
            )}
            <a
              href={primaryHref}
              className="rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-on transition-colors duration-100 hover:bg-accent-hover"
            >
              {primaryLabel}
            </a>
          </div>
        </div>
      </nav>

      <section className="relative overflow-hidden border-b border-line-subtle">
        <div
          className="pointer-events-none absolute inset-0"
          style={{
            background: "radial-gradient(ellipse 60% 50% at 50% 0%, var(--accent-subtle) 0%, transparent 60%)",
            opacity: 0.55,
          }}
          aria-hidden="true"
        />
        <div className="relative mx-auto flex max-w-5xl flex-col items-center gap-5 px-6 pt-20 pb-4 text-center sm:pt-28">
          <h1
            className={`text-4xl leading-[1.1] font-bold tracking-tight text-balance text-ink transition-all duration-700 ease-out sm:text-6xl ${
              entered ? "translate-y-0 opacity-100" : "translate-y-5 opacity-0"
            }`}
          >
            Ship faster. <span className="text-accent">Stay secure.</span>
          </h1>
          <p
            className={`max-w-2xl text-lg leading-relaxed text-ink-secondary transition-all duration-700 ease-out ${
              entered ? "translate-y-0 opacity-100" : "translate-y-5 opacity-0"
            }`}
            style={{ transitionDelay: "100ms" }}
          >
            Describe a bug in plain English. Watch Wrkbase turn it into a structured, triaged ticket
            — before you&apos;d have finished filling out the form.
          </p>
          <div
            className={`flex flex-wrap items-center justify-center gap-3 pt-2 transition-all duration-700 ease-out ${
              entered ? "translate-y-0 opacity-100" : "translate-y-5 opacity-0"
            }`}
            style={{ transitionDelay: "180ms" }}
          >
            <a
              href={primaryHref}
              className="rounded-md bg-accent px-5 py-2.5 text-sm font-semibold text-accent-on transition-all duration-150 hover:-translate-y-0.5 hover:bg-accent-hover hover:shadow-[var(--shadow-card-hover)]"
            >
              {signedIn ? "Go to dashboard" : "Start free trial"}
            </a>
            <a
              href="#pricing"
              className="rounded-md border border-line px-5 py-2.5 text-sm font-medium text-ink transition-colors duration-100 hover:border-line-strong hover:bg-hover"
            >
              See pricing
            </a>
          </div>
          {!signedIn && <p className="text-xs text-ink-tertiary">No credit card required. Free for teams up to 5.</p>}
        </div>

        <div
          className={`relative px-6 pt-8 pb-20 transition-all duration-700 ease-out ${
            entered ? "translate-y-0 opacity-100" : "translate-y-6 opacity-0"
          }`}
          style={{ transitionDelay: "260ms" }}
        >
          <HeroDemo />
        </div>
      </section>

      <section id="features" className="mx-auto w-full max-w-6xl scroll-mt-16 px-6 py-20">
        <div className="mx-auto mb-12 flex max-w-2xl flex-col items-center gap-2 text-center">
          <span className="font-mono text-xs tracking-wider text-accent uppercase">Features</span>
          <h2 className="text-3xl font-bold tracking-tight text-ink text-balance">
            Everything your team needs to plan, build, and ship.
          </h2>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((f) => (
            <FeatureBlock key={f.title} {...f} />
          ))}
        </div>

        <div className="mt-16">
          <BeforeAfter />
        </div>
        <div className="mt-16">
          <LifecycleSequence />
        </div>
      </section>

      <section id="pricing" className="border-y border-line-subtle bg-surface/60">
        <div className="mx-auto w-full max-w-6xl scroll-mt-16 px-6 py-20">
          <div className="mx-auto mb-12 flex max-w-2xl flex-col items-center gap-2 text-center">
            <span className="font-mono text-xs tracking-wider text-accent uppercase">Pricing</span>
            <h2 className="text-3xl font-bold tracking-tight text-ink text-balance">
              Simple pricing, no per-feature surprises.
            </h2>
            <p className="text-sm text-ink-secondary">
              Start free. Upgrade when your team actually needs sprints and AI, not before.
            </p>
          </div>
          <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
            {PLANS.map((plan) => (
              <PlanCard key={plan.name} plan={plan} />
            ))}
          </div>
          <p className="mt-8 text-center text-xs text-ink-tertiary">
            AI features are in early access and expanding regularly. Team and Business prices billed
            monthly per active user.
          </p>
        </div>
      </section>

      <section className="mx-auto w-full max-w-6xl px-6 py-20">
        <div className="mx-auto mb-12 flex max-w-2xl flex-col items-center gap-2 text-center">
          <span className="font-mono text-xs tracking-wider text-accent uppercase">Who it&apos;s for</span>
          <h2 className="text-3xl font-bold tracking-tight text-ink text-balance">
            Built for engineering teams who take their data seriously.
          </h2>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          {AUDIENCE.map((a) => (
            <div key={a.title} className="flex h-full flex-col gap-2.5 rounded-lg border border-line-subtle bg-surface p-6">
              <h3 className="text-[15px] font-semibold text-ink">{a.title}</h3>
              <p className="text-sm leading-relaxed text-ink-secondary">{a.body}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="border-t border-line-subtle">
        <div className="mx-auto flex max-w-6xl flex-col items-center gap-5 px-6 py-20 text-center">
          <h2 className="text-3xl font-bold tracking-tight text-ink text-balance">Ready to ship faster?</h2>
          <p className="max-w-md text-sm text-ink-secondary">
            Start free — no credit card required. Upgrade whenever your team&apos;s ready.
          </p>
          <a
            href={primaryHref}
            className="rounded-md bg-accent px-5 py-2.5 text-sm font-semibold text-accent-on transition-all duration-150 hover:-translate-y-0.5 hover:bg-accent-hover hover:shadow-[var(--shadow-card-hover)]"
          >
            {signedIn ? "Go to dashboard" : "Start free trial"}
          </a>
        </div>
      </section>

      <footer className="border-t border-line-subtle">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-3 px-6 py-8 text-xs text-ink-tertiary sm:flex-row">
          <span>© {new Date().getFullYear()} Wrkbase.</span>
          <div className="flex items-center gap-4">
            <a href="#features" className="transition-colors duration-100 hover:text-ink-secondary">
              Features
            </a>
            <a href="#pricing" className="transition-colors duration-100 hover:text-ink-secondary">
              Pricing
            </a>
            <a href="https://github.com/cl0ud08/wrkbase" className="transition-colors duration-100 hover:text-ink-secondary">
              For developers
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}
