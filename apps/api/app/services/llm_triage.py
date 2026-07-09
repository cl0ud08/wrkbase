"""Real ticket triage: Groq first, Gemini as fallback on any failure. See
worker/main.py for how a result (or the exhaustion of both providers) gets
written back to a ticket, and app/services/llm_client.py for the shared
Groq/Gemini calling mechanics this module and app/services/ticket_parse.py
both build on.

--- Structured output: why json_object + Pydantic, not Groq's own
    json_schema mode -----------------------------------------------

Checked empirically against the real API before choosing, not assumed:
Groq's `response_format={"type": "json_schema", ...}` (strict,
schema-enforced structured output — the stronger guarantee, closer to
OpenAI's structured outputs) returns a 400 for llama-3.3-70b-versatile —
"This model does not support response format `json_schema`." Confirmed
directly by trying it. Groq's strict mode is real, but only on a subset
of models, and the fast, cost-effective, well-established model this
slice actually wants isn't one of them. `{"type": "json_object"}` (loose
JSON mode — guarantees syntactically valid JSON, not schema conformance)
works fine on this model, confirmed the same way. So the exact schema is
spelled out in the system prompt instead, and the response is parsed and
validated through TriageResult regardless — the same Pydantic gate
either provider's raw output has to pass, since json_object mode alone
only proves the output parses as JSON, not that it matches the shape
this app actually needs.

Gemini's `response_schema` (real, enforced structured output) was also
checked, not assumed — and does work as documented. Worth noting: the
first model tried, gemini-2.0-flash, returned a 429 with the free tier's
request quota reported as a literal 0 for that specific model on this
key — a real, current constraint, not a hypothetical. gemini-2.5-flash
works. Both providers' raw output still goes through the identical
TriageResult validation either way — Gemini's stronger guarantee doesn't
change that, since "the SDK enforced it" and "this app independently
confirmed it" are different claims, and only the second one is a claim
this app is actually able to stand behind.
"""

from pydantic import BaseModel, Field, field_validator

from app.db.models import TicketPriority
from app.services.llm_client import LLMCallFailed, call_llm
from app.services.llm_labels import clean_labels

_SYSTEM_PROMPT = """You are a triage assistant for a software engineering issue tracker. Given a ticket's title and description, respond with ONLY a JSON object matching this exact shape — no markdown, no code fences, no text outside the JSON:

{
  "priority": "low" | "medium" | "high" | "critical",
  "labels": [string, ...],
  "reasoning": string
}

Priority guidance:
- critical: data loss, a security vulnerability, a production outage, or a complete blocker for many users
- high: a significant bug or urgent feature with real user impact, short of a full outage
- medium: a normal bug or feature — the default for most tickets
- low: a minor issue, a cosmetic problem, or a nice-to-have

labels: 0 to 5 short, lowercase, single-or-hyphenated-word labels (e.g. "bug", "security", "frontend", "performance"). Omit if nothing fits.

reasoning: exactly one sentence explaining the priority you chose."""

_GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "priority": {
            "type": "STRING",
            "enum": [p.value for p in TicketPriority],
        },
        "labels": {"type": "ARRAY", "items": {"type": "STRING"}},
        "reasoning": {"type": "STRING"},
    },
    "required": ["priority", "labels", "reasoning"],
}


class TriageFailed(Exception):
    """Both providers were tried, up to their retry budgets, and neither
    produced a valid result. Terminal — see worker/main.py for why this
    is written to the ticket as a real failure state, not requeued.
    """


class TriageResult(BaseModel):
    priority: TicketPriority
    labels: list[str] = Field(default_factory=list)
    reasoning: str = Field(min_length=1)

    @field_validator("labels")
    @classmethod
    def _validate_labels(cls, value: list[str]) -> list[str]:
        # Every raw label the model returns is validated here, regardless
        # of which provider produced it (or whether that provider's own
        # structured-output mode was supposed to already guarantee shape)
        # — never trust the model's output blindly, no matter how strong
        # the provider's own guarantee claims to be.
        return clean_labels(value)


def _user_prompt(title: str, description: str | None) -> str:
    lines = [f"Title: {title}"]
    lines.append(f"Description: {description}" if description else "Description: (none provided)")
    return "\n".join(lines)


async def triage_ticket(title: str, description: str | None) -> tuple[TriageResult, str]:
    """Returns (result, provider_name). Raises TriageFailed once both
    providers have exhausted their attempt budget (see
    app/services/llm_client.py) — never retries beyond that, and never
    partially trusts a result that failed TriageResult's validation.
    """
    try:
        return await call_llm(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_user_prompt(title, description),
            response_model=TriageResult,
            gemini_schema=_GEMINI_SCHEMA,
        )
    except LLMCallFailed as exc:
        raise TriageFailed(str(exc)) from exc
