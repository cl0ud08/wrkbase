"""The AI Security Champion's LLM half: once app/services/appsec_triggers.py's
keyword gate has already matched one or more categories on a ticket
(cheaply, deterministically, with no LLM involved), this module calls a
real LLM exactly once to write a *tailored* review comment applying
those categories' own checklists to what the ticket specifically says —
not to decide whether the ticket is security-relevant at all. See
app/api/tickets.py and worker/main.py for how the two halves compose.

Reuses app/services/llm_client.py's shared Groq-primary/Gemini-fallback
calling core (timeout, retry budget, provider fallback, response
validation against a Pydantic model) — the same module llm_triage.py
and ticket_parse.py already build on. This is the third real call site,
which is exactly the kind of reuse that module was extracted for two
slices ago.
"""

from pydantic import BaseModel, Field, field_validator

from app.services.appsec_triggers import TRIGGER_CATEGORIES, TriggerCategory
from app.services.llm_client import LLMCallFailed, call_llm

_VALID_CATEGORY_KEYS = frozenset(c.key for c in TRIGGER_CATEGORIES)

_GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "comment": {"type": "STRING"},
        "categories_addressed": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["comment", "categories_addressed"],
}


class AppSecReviewFailed(Exception):
    """Both providers were tried, up to their retry budgets, and neither
    produced a usable review comment. Terminal — see worker/main.py: the
    ticket stays flagged (the keyword match already decided that,
    synchronously, before this was ever called) with
    appsec_review_status='failed' and a real appsec_review_error, not
    silently un-flagged.
    """


class AppSecReview(BaseModel):
    comment: str = Field(min_length=1)
    categories_addressed: list[str] = Field(min_length=1)

    @field_validator("categories_addressed")
    @classmethod
    def _validate_categories(cls, value: list[str]) -> list[str]:
        # Never trust the model's own claim about which categories it
        # addressed any more than its prose — reject a hallucinated
        # category key outright rather than writing it to the ticket.
        # This checks against the full, fixed universe of real category
        # keys (5 total), not the specific subset requested for this one
        # call — a genuine, if narrower, guard against a malformed
        # response, without needing per-call context threaded through
        # llm_client.py's generic response_model validation hook.
        unknown = [key for key in value if key not in _VALID_CATEGORY_KEYS]
        if unknown:
            raise ValueError(f"unknown category key(s) in categories_addressed: {unknown}")
        return value


def _system_prompt(categories: list[TriggerCategory]) -> str:
    lines = [
        "You are an application security reviewer for a software engineering issue tracker.",
        "A ticket has been automatically flagged for the following security-relevant categories, "
        "based on keyword matching against its title and description:",
        "",
    ]
    for category in categories:
        lines.append(f"### {category.label} ({category.key})")
        lines.extend(f"- {item}" for item in category.checklist)
        lines.append("")
    lines.append(
        "Given the ticket's title and description below, write ONE short, tailored security review "
        "comment covering all the categories above. Apply the relevant checklist items specifically to "
        "what THIS ticket actually describes — reference concrete details from the ticket (the specific "
        "feature, data, or flow it mentions), not a generic restatement of the checklist. Skip checklist "
        "items that clearly don't apply to this specific ticket rather than listing everything regardless "
        "of relevance. Keep it to a few sentences to a short paragraph — a busy engineer should be able to "
        "read it in under 30 seconds.\n\n"
        "Respond with ONLY a JSON object matching this exact shape — no markdown, no code fences, no text "
        "outside the JSON:\n\n"
        "{\n"
        '  "comment": string,\n'
        '  "categories_addressed": [string, ...]\n'
        "}\n\n"
        "categories_addressed: the category keys (e.g. \"file_upload\") your comment actually covers — "
        "normally all of the categories listed above, but omit one only if it's genuinely inapplicable to "
        "this specific ticket, and say so in the comment."
    )
    return "\n".join(lines)


def _user_prompt(title: str, description: str | None) -> str:
    lines = [f"Title: {title}"]
    lines.append(f"Description: {description}" if description else "Description: (none provided)")
    return "\n".join(lines)


async def generate_appsec_review(
    title: str, description: str | None, categories: list[TriggerCategory]
) -> tuple[AppSecReview, str]:
    """Returns (review, provider_name). Raises AppSecReviewFailed once both
    providers have exhausted their attempt budget (see
    app/services/llm_client.py) — never retries beyond that, and never
    partially trusts a result that failed AppSecReview's validation.
    """
    try:
        return await call_llm(
            system_prompt=_system_prompt(categories),
            user_prompt=_user_prompt(title, description),
            response_model=AppSecReview,
            gemini_schema=_GEMINI_SCHEMA,
        )
    except LLMCallFailed as exc:
        raise AppSecReviewFailed(str(exc)) from exc
