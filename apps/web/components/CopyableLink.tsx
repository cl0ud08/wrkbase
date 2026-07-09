"use client";

import { useRef, useState } from "react";

// One shared component for every "here's a link, dev mode" moment in this
// app — invite links, password-reset links, email-verification links are
// all the same underlying pattern (an opaque, single-use token in a URL,
// meant to be copied and shared once) with the exact same display gap: no
// way to copy it without a manual triple-click-and-select, and no way to
// see it again once it scrolls off screen. One implementation here instead
// of three hand-rolled ones that could drift out of sync with each other.
export default function CopyableLink({ link }: { link: string }) {
  const [copied, setCopied] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  async function handleCopy() {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(link);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
        return;
      } catch {
        // Falls through to the manual-select fallback below — e.g. the
        // user denied clipboard permission after the browser prompted.
      }
    }
    // Fallback for a non-HTTPS context (the Clipboard API's writeText is
    // only exposed in a secure context) or a browser that never had it:
    // select the input's text so Ctrl+C/Cmd+C works, rather than the
    // button silently doing nothing. document.execCommand('copy') is
    // deprecated and inconsistent enough across browsers that a manual
    // select-then-copy is the more honest fallback, not a fragile one.
    inputRef.current?.select();
  }

  return (
    <div className="flex items-center gap-2">
      <input
        ref={inputRef}
        type="text"
        readOnly
        value={link}
        onFocus={(e) => e.currentTarget.select()}
        className="min-w-0 flex-1 truncate rounded-md border border-line bg-surface-2 px-2 py-1 font-mono text-xs text-ink"
      />
      <button
        type="button"
        onClick={handleCopy}
        className="flex-shrink-0 rounded-md border border-line px-2.5 py-1 text-xs font-medium text-ink transition-colors duration-100 hover:border-line-strong hover:bg-hover"
      >
        {copied ? "Copied!" : "Copy"}
      </button>
    </div>
  );
}
