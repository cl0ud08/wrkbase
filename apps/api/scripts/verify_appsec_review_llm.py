"""Hits the REAL Groq and Gemini APIs — costs real API credits and is
inherently non-deterministic, which is exactly why this is NOT wired
into CI. See verify_triage.py's module docstring for the full real-vs-
stub reasoning, and scripts/verify_appsec_triggers.py's own docstring
for why the CI-wired proof runs against a local stub instead of this
script specifically: the stub can prove the plumbing carries a comment
through correctly, but it has no real language understanding, so it
cannot prove a comment is genuinely *tailored* to a specific ticket
rather than a templated dump of the matched category's checklist.
That's what this script actually checks, and it can only mean anything
against the real API.

Run manually, locally, whenever you actually want to confirm the real
integration still writes tailored guidance, not templated boilerplate:

    docker compose exec api python -m scripts.verify_appsec_review_llm

Three concrete, checkable signals of "tailored," not just eyeballing
the output and asserting it looks right:

1. Two different tickets in the SAME trigger category produce
   genuinely different comments — a templated response would be near-
   identical regardless of what the ticket actually says; a tailored
   one reflects each ticket's own specific feature.
2. Each comment references at least one concrete, ticket-specific term
   that appears in that ticket's own title/description but not in the
   category's own checklist wording — proof the model read the ticket,
   not just the category label.
3. No checklist item's full sentence appears verbatim in the comment —
   the model is asked to paraphrase and apply, not copy-paste the
   static list it was given.
"""

import asyncio
from unittest.mock import patch

from app.services.appsec_review import AppSecReviewFailed, generate_appsec_review
from app.services.appsec_triggers import match_triggers


async def main() -> None:
    # --- two different file_upload tickets should read differently -------
    ticket_a = (
        "Add profile picture upload",
        "Users upload a JPG or PNG as their avatar, shown on their profile page.",
    )
    ticket_b = (
        "Add CSV import for bulk ticket creation",
        "Project admins can upload a CSV file to create many tickets at once.",
    )
    categories_a = match_triggers(*ticket_a)
    categories_b = match_triggers(*ticket_b)
    assert {c.key for c in categories_a} == {"file_upload"}
    assert {c.key for c in categories_b} == {"file_upload"}

    review_a, provider_a = await generate_appsec_review(*ticket_a, categories_a)
    review_b, provider_b = await generate_appsec_review(*ticket_b, categories_b)

    assert review_a.comment.strip().lower() != review_b.comment.strip().lower(), (
        "two different tickets in the same category produced an identical comment -- "
        "looks templated, not tailored"
    )
    print(f"PASS: two different file_upload tickets (via {provider_a}, {provider_b}) produce different comments")

    # Each comment should reference something specific to *its own* ticket
    # -- a word that shows up in that ticket's own text but has no reason
    # to appear in a generic checklist restatement.
    assert "avatar" in review_a.comment.lower() or "profile" in review_a.comment.lower() or "jpg" in review_a.comment.lower() or "png" in review_a.comment.lower(), (
        f"expected the avatar-upload ticket's comment to reference its own specifics, got: {review_a.comment!r}"
    )
    assert "csv" in review_b.comment.lower() or "bulk" in review_b.comment.lower() or "import" in review_b.comment.lower(), (
        f"expected the CSV-import ticket's comment to reference its own specifics, got: {review_b.comment!r}"
    )
    print("PASS: each comment references concrete details specific to its own ticket, not generic phrasing")

    # No raw checklist sentence should appear verbatim -- the model was
    # asked to apply and paraphrase, not copy-paste the static list.
    file_upload_category = next(c for c in categories_a if c.key == "file_upload")
    for item in file_upload_category.checklist:
        assert item.lower() not in review_a.comment.lower(), (
            f"a raw checklist item appeared verbatim in the comment, looks pasted not tailored: {item!r}"
        )
    print("PASS: no checklist item appears verbatim -- the comment paraphrases and applies, not pastes")

    # --- a real Gemini fallback still produces tailored output -----------
    async def broken_groq(*args: object, **kwargs: object) -> None:
        raise RuntimeError("deliberately broken for this check")

    with patch("app.services.llm_client._call_groq", broken_groq):
        title = "Allow admins to grant admin role to other members"
        description = "Add a UI control and endpoint for promoting a member to admin."
        categories = match_triggers(title, description)
        assert {c.key for c in categories} == {"admin_permission"}
        review, provider = await generate_appsec_review(title, description, categories)
        assert provider == "gemini", f"expected the Gemini fallback to serve this, got {provider}"
        assert "admin" in review.comment.lower()
        print("PASS: with Groq broken, the real Gemini fallback produces a real, relevant comment too")

    # --- both providers failing is a real, terminal AppSecReviewFailed ---
    async def always_broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("deliberately broken for this check")

    with patch("app.services.llm_client._call_groq", always_broken), patch(
        "app.services.llm_client._call_gemini", always_broken
    ):
        categories = match_triggers("Add file upload", "Let users upload a file.")
        try:
            await generate_appsec_review("Add file upload", "Let users upload a file.", categories)
        except AppSecReviewFailed:
            print("PASS: both providers failing raises AppSecReviewFailed, the real terminal-failure path")
        else:
            raise AssertionError("expected AppSecReviewFailed when both providers are broken")

    print("\nAll real-LLM AppSec review checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
