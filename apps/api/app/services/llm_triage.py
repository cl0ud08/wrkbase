"""Real ticket triage: Groq first, Gemini as fallback on any failure. See
worker/main.py for how a result (or the exhaustion of both providers) gets
written back to a ticket.

--- Structured output: why json_object + Pydantic validation, not Groq's
    own json_schema mode -----------------------------------------------

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

import asyncio
import logging

from google import genai
from google.genai import types as genai_types
from groq import AsyncGroq
from pydantic import BaseModel, Field, field_validator

from app.core.config import settings
from app.db.models import TicketPriority

logger = logging.getLogger("worker.triage.llm")

_GROQ_MODEL = "llama-3.3-70b-versatile"
_GEMINI_MODEL = "gemini-2.5-flash"

# Don't let a hung provider call hold a worker (and the RabbitMQ message
# it's processing) indefinitely — see worker/main.py's module docstring
# on why an unacked message sitting forever is its own kind of problem.
_CALL_TIMEOUT_SECONDS = 10.0

# Bounds the *total* number of real LLM calls one job can ever cost: at
# most this many tries against Groq, then at most this many against
# Gemini, then a terminal failure — never nack-and-requeue back through
# RabbitMQ on an LLM failure (see worker/main.py), which would restart
# this same budget from zero on every redelivery and could burn API spend
# indefinitely on a job that's genuinely never going to succeed.
_MAX_ATTEMPTS_PER_PROVIDER = 2

_MAX_LABELS = 5
_MAX_LABEL_LENGTH = 30

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
    def _clean_labels(cls, value: list[str]) -> list[str]:
        # Every raw label the model returns is validated here, regardless
        # of which provider produced it (or whether that provider's own
        # structured-output mode was supposed to already guarantee shape)
        # — never trust the model's output blindly, no matter how strong
        # the provider's own guarantee claims to be.
        cleaned = [label.strip().lower() for label in value if label.strip()]
        if len(cleaned) > _MAX_LABELS:
            raise ValueError(f"too many labels ({len(cleaned)} > {_MAX_LABELS})")
        for label in cleaned:
            if len(label) > _MAX_LABEL_LENGTH:
                raise ValueError(f"label {label!r} exceeds {_MAX_LABEL_LENGTH} characters")
        return cleaned


def _user_prompt(title: str, description: str | None) -> str:
    lines = [f"Title: {title}"]
    lines.append(f"Description: {description}" if description else "Description: (none provided)")
    return "\n".join(lines)


async def _call_groq(title: str, description: str | None) -> TriageResult:
    client = AsyncGroq(api_key=settings.groq_api_key, base_url=settings.groq_base_url)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=_GROQ_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(title, description)},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        ),
        timeout=_CALL_TIMEOUT_SECONDS,
    )
    content = response.choices[0].message.content
    if content is None:
        raise ValueError("Groq returned an empty completion")
    return TriageResult.model_validate_json(content)


async def _call_gemini(title: str, description: str | None) -> TriageResult:
    http_options = (
        genai_types.HttpOptions(base_url=settings.gemini_base_url) if settings.gemini_base_url else None
    )
    client = genai.Client(api_key=settings.gemini_api_key, http_options=http_options)
    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=_GEMINI_MODEL,
            contents=_user_prompt(title, description),
            config=genai_types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema={
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
                },
                temperature=0.2,
            ),
        ),
        timeout=_CALL_TIMEOUT_SECONDS,
    )
    if not response.text:
        raise ValueError("Gemini returned an empty response")
    return TriageResult.model_validate_json(response.text)


async def triage_ticket(title: str, description: str | None) -> tuple[TriageResult, str]:
    """Returns (result, provider_name). Raises TriageFailed once both
    providers have exhausted _MAX_ATTEMPTS_PER_PROVIDER — never retries
    beyond that budget, and never partially trusts a result that failed
    TriageResult's validation (a malformed-but-parseable response counts
    as a failed attempt, exactly like a timeout or a rate limit, not a
    result written half-validated).
    """
    for provider_name, call in (("groq", _call_groq), ("gemini", _call_gemini)):
        for attempt in range(1, _MAX_ATTEMPTS_PER_PROVIDER + 1):
            try:
                result = await call(title, description)
            except Exception as exc:
                logger.warning(
                    "%s triage attempt %d/%d failed: %s: %s",
                    provider_name,
                    attempt,
                    _MAX_ATTEMPTS_PER_PROVIDER,
                    type(exc).__name__,
                    exc,
                )
                continue
            return result, provider_name

    raise TriageFailed(
        f"both providers exhausted their {_MAX_ATTEMPTS_PER_PROVIDER}-attempt budget"
    )
