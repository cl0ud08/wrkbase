"""Empirical threshold tuning for semantic duplicate detection — hits
the REAL Gemini embedding API, costs real credits, and is not wired into
CI for the same reasons verify_triage_llm.py and
scripts/verify_ticket_parse_llm.py-equivalent real-API checks never are
(see verify_triage.py's module docstring for the full real-vs-stub
reasoning this project has used consistently since Phase 3 slice 2).

This is not a proof script asserting pass/fail — it's the actual work
behind app/services/ticket_duplicates.py's SIMILARITY_THRESHOLD = 0.83,
kept in the repo so that number is reproducible and re-checkable, not a
one-off number that only ever existed in a throwaway shell session.
Re-run whenever the embedding model, dimension, or prompt-adjacent text
shape changes, to confirm the threshold still holds.

Run: docker compose exec api python -m scripts.tune_duplicate_threshold

--- What was tested and what it showed --------------------------------

13 real ticket-title/description pairs across three categories:

  SIMILAR (same bug/feature, reworded) — 5 pairs, e.g. "Login fails on
  Safari" / "Safari login broken"; "Password reset email never
  arrives" / "Reset password email not being delivered". Real cosine
  similarities: 0.8351, 0.8395, 0.8752, 0.9159, 0.9185. Floor: 0.8351.

  DIFFERENT (genuinely unrelated) — 4 pairs, e.g. "Login fails on
  Safari" / "Add CSV export to reports page". Real cosine
  similarities: 0.4632, 0.4991, 0.5051, 0.5207. Ceiling: 0.5207.

  ADJACENT (same category, different specific issue) — 4 pairs, e.g.
  "Password reset email never arrives" / "Verification email never
  arrives"; "Export tickets to CSV" / "Export tickets to PDF"; "Add
  dark mode toggle" / "Add light mode toggle"; "Login fails on
  Safari" / "Login fails on Firefox". Real cosine similarities:
  0.7921, 0.8291, 0.8873, 0.9300 — a spread that genuinely overlaps
  both the SIMILAR and DIFFERENT clusters.

SIMILAR and DIFFERENT are cleanly, unambiguously separated (a >0.3 gap
between 0.5207 and 0.8351) — semantic embeddings are clearly doing
their job at the topic level. ADJACENT is the real finding: no fixed
threshold can perfectly tell "the same request, reworded" apart from "a
closely related but genuinely different one," because topic-level
semantic similarity doesn't encode that distinction at all — "password
reset" and "email verification" are topically almost the same thing
(an email that doesn't arrive) even though they're different bugs.
That's exactly why this feature is a non-blocking warning (point 5 of
its own brief), not a hard gate: an occasional flag on a merely-related
ticket costs a human one glance, not a blocked action.

0.83 sits just below the observed SIMILAR floor (catches every tested
genuine duplicate), enormously above the observed DIFFERENT ceiling
(excludes every tested unrelated pair, large margin), and inside the
ADJACENT cluster's own real spread — which is exactly where a threshold
belongs, given that cluster's scores genuinely straddle both of the
others.
"""

import asyncio

from app.services.ticket_embedding import generate_embedding

# (category, title_a, description_a, title_b, description_b)
_PAIRS: list[tuple[str, str, str, str, str]] = [
    (
        "SIMILAR",
        "Login fails on Safari",
        "Users cannot log in using the Safari browser on macOS.",
        "Safari login broken",
        "Customers report being unable to sign in when using Safari.",
    ),
    (
        "SIMILAR",
        "App crashes on startup for iOS 17 users",
        "The mobile app crashes immediately after opening on devices running iOS 17.",
        "iOS 17 users see crash on launch",
        "On iOS 17 the app terminates unexpectedly right when it is launched.",
    ),
    (
        "SIMILAR",
        "Add dark mode toggle to settings",
        "Users have requested a dark mode option in the settings page.",
        "Implement a dark theme switch in settings",
        "We should let users switch to a dark theme from the settings page.",
    ),
    (
        "SIMILAR",
        "Export tickets to CSV",
        "Let users download their tickets as a CSV file from the project page.",
        "CSV export for the ticket list",
        "Team wants a way to export the current ticket list to a CSV.",
    ),
    (
        "SIMILAR",
        "Password reset email never arrives",
        "Some users report never receiving the password reset email after requesting one.",
        "Reset password email not being delivered",
        "A subset of users say the reset email never shows up in their inbox.",
    ),
    (
        "DIFFERENT",
        "Login fails on Safari",
        "Users cannot log in using the Safari browser on macOS.",
        "Add CSV export to reports page",
        "Allow users to export their report data as a CSV file.",
    ),
    (
        "DIFFERENT",
        "App crashes on startup for iOS 17 users",
        "The mobile app crashes immediately after opening on devices running iOS 17.",
        "Update pricing page copy for the new tier",
        "Marketing wants new copy on the pricing page for the enterprise tier.",
    ),
    (
        "DIFFERENT",
        "Add dark mode toggle to settings",
        "Users have requested a dark mode option in the settings page.",
        "Database backup job fails intermittently on Sundays",
        "The nightly backup cron job fails about once a month, usually on Sundays.",
    ),
    (
        "DIFFERENT",
        "Password reset email never arrives",
        "Some users report never receiving the password reset email after requesting one.",
        "Sprint velocity chart is wrong",
        "The velocity chart on the sprint report page shows incorrect numbers.",
    ),
    (
        "ADJACENT",
        "Add dark mode toggle to settings",
        "Users have requested a dark mode option in the settings page.",
        "Add light mode toggle to settings",
        "Users have requested a light mode option in the settings page.",
    ),
    (
        "ADJACENT",
        "Login fails on Safari",
        "Users cannot log in using the Safari browser on macOS.",
        "Login fails on Firefox",
        "Users cannot log in using the Firefox browser on Windows.",
    ),
    (
        "ADJACENT",
        "Password reset email never arrives",
        "Some users report never receiving the password reset email after requesting one.",
        "Verification email never arrives",
        "Some users report never receiving the email verification email after signing up.",
    ),
    (
        "ADJACENT",
        "Export tickets to CSV",
        "Let users download their tickets as a CSV file from the project page.",
        "Export tickets to PDF",
        "Let users download their tickets as a PDF file from the project page.",
    ),
]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot / (norm_a * norm_b)


async def main() -> None:
    from app.services.ticket_duplicates import SIMILARITY_THRESHOLD

    results: dict[str, list[float]] = {"SIMILAR": [], "DIFFERENT": [], "ADJACENT": []}

    for category, title_a, desc_a, title_b, desc_b in _PAIRS:
        embedding_a = await generate_embedding(title_a, desc_a)
        embedding_b = await generate_embedding(title_b, desc_b)
        similarity = _cosine(embedding_a, embedding_b)
        results[category].append(similarity)
        flagged = "FLAGGED" if similarity >= SIMILARITY_THRESHOLD else "not flagged"
        print(f"{category:10s} sim={similarity:.4f}  {flagged:12s}  {title_a!r} vs {title_b!r}")

    print()
    for category, scores in results.items():
        print(f"{category}: min={min(scores):.4f} max={max(scores):.4f} n={len(scores)}")

    assert max(results["DIFFERENT"]) < SIMILARITY_THRESHOLD, "a DIFFERENT pair scored above threshold"
    assert min(results["SIMILAR"]) >= SIMILARITY_THRESHOLD, "a SIMILAR pair scored below threshold"
    print(f"\nSIMILARITY_THRESHOLD={SIMILARITY_THRESHOLD}: every SIMILAR pair caught, every DIFFERENT pair excluded.")


if __name__ == "__main__":
    asyncio.run(main())
