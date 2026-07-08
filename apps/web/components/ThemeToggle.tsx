"use client";

import { useTheme } from "../lib/theme-context";

export default function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const { theme, toggleTheme } = useTheme();

  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
      className={`flex items-center gap-2 rounded-md border border-line text-ink-secondary transition-colors duration-100 hover:border-line-strong hover:text-ink ${
        compact ? "px-2.5 py-1 text-xs" : "px-3.5 py-2.5 text-sm font-medium"
      }`}
    >
      <span
        className="h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_6px_var(--accent)]"
        aria-hidden="true"
      />
      <span className="font-mono tracking-wide">{theme === "dark" ? "Dark" : "Light"}</span>
    </button>
  );
}
