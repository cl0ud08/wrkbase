"""Natural-language ticket parsing: given raw text a user typed instead of
filling in a form, ask the model to extract structured ticket fields --
or say honestly that it can't. See app/api/tickets.py's POST .../parse
endpoint for how this gets exposed, and app/services/llm_client.py for the
shared Groq/Gemini calling mechanics this module and llm_triage.py both
build on.

This is a *synchronous* call, unlike llm_triage.py's async, RabbitMQ-
mediated one -- see app/api/tickets.py's parse endpoint docstring for why
that's the right call here specifically, not just a different style
choice.

Confidence is a first-class part of the response shape, not an
afterthought: ParsedTicketCandidate.confident distinguishes "the model
looked at this and genuinely can't tell" from a result that just happens
to be wrong, and the model is explicitly instructed never to fabricate a
title or type to make a low-confidence response look complete. See the
model's own docstring below and the endpoint for what the frontend does
with confident=False.
"""

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import TicketPriority, TicketType
from app.services.llm_client import LLMCallFailed, call_llm
from app.services.llm_labels import clean_labels

_SYSTEM_PROMPT = """You are a ticket-parsing assistant for a software engineering issue tracker. Given a short, informal, natural-language request describing a piece of work, extract structured ticket fields -- or honestly say you can't.

Respond with ONLY a JSON object matching this exact shape — no markdown, no code fences, no text outside the JSON:

{
  "confident": true | false,
  "title": string or null,
  "description": string or null,
  "type": "epic" | "story" | "task" or null,
  "priority": "low" | "medium" | "high" | "critical" or null,
  "labels": [string, ...],
  "clarification": string or null
}

If, and only if, you can confidently identify a clear, actionable title and a ticket type from the input, set "confident": true and fill in "title" and "type" (both required when confident is true). Never choose "subtask" for type -- a subtask needs a specific parent ticket, which free text alone can't determine; use "task" instead if the work sounds granular.

If the input is too vague, ambiguous, doesn't actually describe a piece of work, or you genuinely can't tell what the title or type should be, set "confident": false, leave "title" and "type" null, and explain what's missing or unclear in "clarification" (required when confident is false) -- a short, specific, human-readable sentence, not a generic apology. Do not fabricate a title or type just to make the response look complete.

"description": a slightly expanded restatement of the request, or null if the title already says everything.

"priority": only set this if the input itself signals urgency or importance (e.g. "high priority", "ASAP", "minor", "whenever"); otherwise leave it null -- do not invent a priority the input never implied. This is a preview only; the ticket's real priority is determined independently once it's actually created.

"labels": 0 to 5 short, lowercase, single-or-hyphenated-word labels (e.g. "bug", "security", "frontend", "performance"). Omit if nothing fits."""

_GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "confident": {"type": "BOOLEAN"},
        "title": {"type": "STRING", "nullable": True},
        "description": {"type": "STRING", "nullable": True},
        "type": {"type": "STRING", "enum": ["epic", "story", "task"], "nullable": True},
        "priority": {"type": "STRING", "enum": [p.value for p in TicketPriority], "nullable": True},
        "labels": {"type": "ARRAY", "items": {"type": "STRING"}},
        "clarification": {"type": "STRING", "nullable": True},
    },
    "required": ["confident", "labels"],
}


class TicketParseUnavailable(Exception):
    """Both providers were tried, up to their retry budgets, and neither
    produced a usable response -- a real provider/infrastructure failure.
    Deliberately distinct from ParsedTicketCandidate.confident=False,
    which is a successful, honest response saying the *input* was too
    ambiguous, not that anything is broken. See app/api/tickets.py's
    parse endpoint for the different HTTP treatment each gets.
    """


class ParsedTicketCandidate(BaseModel):
    """confident=True guarantees title and type are both present and
    type is never "subtask" (see the validator below). confident=False
    guarantees clarification is present and makes no promise about any
    other field -- the model may still have offered partial guesses, but
    nothing here treats them as reliable; see the parse endpoint and
    frontend for why confident=False fields aren't used to pre-fill
    anything.
    """

    confident: bool
    title: str | None = Field(default=None, max_length=200)
    description: str | None = None
    type: TicketType | None = None
    priority: TicketPriority | None = None
    labels: list[str] = Field(default_factory=list)
    clarification: str | None = None

    @field_validator("labels")
    @classmethod
    def _validate_labels(cls, value: list[str]) -> list[str]:
        return clean_labels(value)

    @model_validator(mode="after")
    def _check_confidence_shape(self) -> "ParsedTicketCandidate":
        # Never trust the model's own "confident" flag at face value
        # either -- a response that claims confident=true but is missing
        # the fields that promise implies, or claims confident=false with
        # no explanation, fails validation exactly like a malformed
        # response would, and counts as a failed attempt in call_llm's
        # retry loop (see llm_client.py), not a half-trusted result.
        if self.confident:
            if not self.title or self.type is None:
                raise ValueError("confident=true requires both title and type")
            if self.type == TicketType.SUBTASK:
                raise ValueError(
                    "type must be epic, story, or task -- a subtask needs a specific parent "
                    "ticket, which free text alone can't determine"
                )
        elif not self.clarification:
            raise ValueError("confident=false requires a clarification message")
        return self


def _user_prompt(text: str) -> str:
    return f"Request: {text}"


async def parse_ticket_text(text: str) -> tuple[ParsedTicketCandidate, str]:
    """Returns (candidate, provider_name). Raises TicketParseUnavailable
    once both providers have exhausted their attempt budget (see
    app/services/llm_client.py) -- distinct from candidate.confident being
    False, which is a normal, successful return.
    """
    try:
        return await call_llm(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_user_prompt(text),
            response_model=ParsedTicketCandidate,
            gemini_schema=_GEMINI_SCHEMA,
        )
    except LLMCallFailed as exc:
        raise TicketParseUnavailable(str(exc)) from exc
