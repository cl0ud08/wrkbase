"""Hits the REAL Groq and Gemini APIs — costs real API credits and is
inherently non-deterministic, which is exactly why this is NOT wired into
CI. See verify_triage.py's module docstring for the full reasoning on why
the CI-wired proof runs against a local stub instead of this script.

Run manually, locally, whenever you actually want to confirm the real
integration still works — after touching the prompt, after a provider
changes model availability (this app already hit that once: gemini-2.0
-flash returned a 429 with a literal 0-request free-tier quota for this
key; gemini-2.5-flash is what's actually pinned), or just periodically:

    docker compose exec api python -m scripts.verify_triage_llm

Not a proof of exact output — an LLM's classification of a given ticket
is not byte-for-byte reproducible the way this project's other proofs
are, and asserting it were would make this script flaky for reasons
that have nothing to do with whether the integration is broken. What IS
asserted: the response is well-formed and passes TriageResult's real
validation, and a ticket description strongly suggestive of one priority
band doesn't come back classified in the opposite one — a sanity check
on the integration actually working, not a grade on model quality.
"""

import asyncio
from unittest.mock import patch

from app.services.llm_triage import TicketPriority, TriageFailed, triage_ticket


async def main() -> None:
    # --- a clearly severe ticket should land in the upper priority band --
    severe, provider = await triage_ticket(
        "Production database is down, all customers affected",
        "Started 5 minutes ago, error rate at 100%, on-call paged.",
    )
    assert severe.priority in (TicketPriority.HIGH, TicketPriority.CRITICAL), (
        f"expected high/critical for an outage ticket, got {severe.priority.value}"
    )
    assert severe.reasoning
    print(f"PASS: an outage ticket is classified {severe.priority.value} (via {provider}), reasoning present")

    # --- a clearly trivial ticket should land in the lower priority band -
    trivial, provider2 = await triage_ticket("Fix typo in footer copyright text", None)
    assert trivial.priority in (TicketPriority.LOW, TicketPriority.MEDIUM), (
        f"expected low/medium for a cosmetic ticket, got {trivial.priority.value}"
    )
    assert trivial.reasoning
    print(f"PASS: a cosmetic ticket is classified {trivial.priority.value} (via {provider2}), reasoning present")

    # --- the Gemini fallback genuinely fires against the real Gemini API,
    # not just a stub -- Groq is deliberately broken for this one call
    # only, proving triage_ticket()'s own orchestration falls back
    # correctly, not just that _call_gemini works in isolation.
    async def broken_groq(*args: object, **kwargs: object) -> None:
        raise RuntimeError("deliberately broken for this check")

    with patch("app.services.llm_triage._call_groq", broken_groq):
        result, provider3 = await triage_ticket("Add a dark mode toggle to settings", "Nice to have.")
        assert provider3 == "gemini", f"expected the Gemini fallback to serve this, got {provider3}"
        assert result.reasoning
        print("PASS: with Groq broken, the real Gemini fallback serves a valid result")

    # --- both providers failing is a real, terminal TriageFailed --------
    async def always_broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("deliberately broken for this check")

    with patch("app.services.llm_triage._call_groq", always_broken), patch(
        "app.services.llm_triage._call_gemini", always_broken
    ):
        try:
            await triage_ticket("Anything", None)
        except TriageFailed:
            print("PASS: both providers failing raises TriageFailed, the real terminal-failure path")
        else:
            raise AssertionError("expected TriageFailed when both providers are broken")

    print("\nAll real-LLM integration checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
