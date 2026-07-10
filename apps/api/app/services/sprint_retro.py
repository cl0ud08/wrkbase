"""The AI sprint summary agent's LLM half: given a SprintRetroContext
already assembled by app/services/sprint_retro_context.py (pure data, no
LLM, see that module's docstring for what's honestly available to build
this from), calls a real LLM once to write a structured retro — a short
narrative, what got done, what didn't, and risk flags/notable blockers,
sectioned rather than a wall of text.

Reuses app/services/llm_client.py's shared Groq-primary/Gemini-fallback
calling core — the same module llm_triage.py, ticket_parse.py, and
appsec_review.py already build on. Fourth real call site.
"""

from pydantic import BaseModel, Field

from app.services.llm_client import LLMCallFailed, call_llm
from app.services.sprint_retro_context import SprintRetroContext, TicketSnapshot

_GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "narrative": {"type": "STRING"},
        "completed_highlights": {"type": "ARRAY", "items": {"type": "STRING"}},
        "incomplete_notes": {"type": "ARRAY", "items": {"type": "STRING"}},
        "risks": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["narrative", "completed_highlights", "incomplete_notes", "risks"],
}


class SprintRetroFailed(Exception):
    """Both providers were tried, up to their retry budgets, and neither
    produced a usable retro. Terminal — see worker/main.py: the sprint
    stays completed (retro generation never blocks or reverses the
    completion itself — see app/api/sprints.py's complete_sprint) with
    retro_status='failed' and a real retro_error, not silently stuck on
    'pending' forever.
    """


class SprintRetro(BaseModel):
    narrative: str = Field(min_length=1)
    # Empty lists are a real, expected outcome here, not a validation
    # failure — a fully-completed sprint has nothing to put in
    # incomplete_notes/risks, and a sprint with nothing completed has
    # nothing to put in completed_highlights. See point 5's edge cases.
    completed_highlights: list[str] = Field(default_factory=list)
    incomplete_notes: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


def _format_tickets(tickets: list[TicketSnapshot], empty_label: str) -> str:
    if not tickets:
        return f"  ({empty_label})"
    lines = []
    for t in tickets:
        points = f"{t.story_points} pts" if t.story_points is not None else "unestimated"
        lines.append(f"  - {t.title} ({points})")
    return "\n".join(lines)


def _system_prompt() -> str:
    return (
        "You are an agile coach writing a sprint retrospective summary for a software "
        "engineering team, based only on the real ticket data provided below — never invent "
        "details, blockers, or people not present in that data.\n\n"
        "Write:\n"
        "- narrative: a short (2-4 sentence) plain-language overview of how the sprint went "
        "overall, grounded in the actual completed/planned points and ticket counts given.\n"
        "- completed_highlights: a few bullet points on what actually got finished, referencing "
        "the specific completed tickets' real titles — don't generalize or pad this out if only "
        "one or two tickets finished. Empty list if nothing was completed.\n"
        "- incomplete_notes: a few bullet points on what didn't get finished and was returned to "
        "the backlog, referencing those tickets' real titles. Empty list if nothing was "
        "returned — that's a fully-completed sprint, a good outcome, not something to invent an "
        "issue about.\n"
        "- risks: notable risk flags or blockers worth flagging to the team — for example, a "
        "meaningful gap between planned and completed points, several related tickets all "
        "returned together (a possible scope or dependency problem), or very little completed "
        "relative to what was planned. Empty list if nothing stands out — do not invent a risk "
        "to fill this section.\n\n"
        "Respond with ONLY a JSON object matching this exact shape — no markdown, no code "
        "fences, no text outside the JSON:\n\n"
        "{\n"
        '  "narrative": string,\n'
        '  "completed_highlights": [string, ...],\n'
        '  "incomplete_notes": [string, ...],\n'
        '  "risks": [string, ...]\n'
        "}"
    )


def _user_prompt(context: SprintRetroContext) -> str:
    lines = [
        f"Sprint: {context.sprint_name}",
        f"Goal: {context.sprint_goal or '(none set)'}",
        f"Dates: {context.start_date} to {context.end_date}",
        f"Points completed: {context.points_done} / {context.points_planned} planned",
        "",
        f"Completed tickets ({len(context.completed)}):",
        _format_tickets(context.completed, "nothing was completed this sprint"),
        "",
        f"Returned to backlog, not finished ({len(context.returned)}):",
        _format_tickets(context.returned, "nothing was returned — everything planned was finished"),
    ]
    return "\n".join(lines)


async def generate_sprint_retro(context: SprintRetroContext) -> tuple[SprintRetro, str]:
    """Returns (retro, provider_name). Raises SprintRetroFailed once both
    providers have exhausted their attempt budget.
    """
    try:
        return await call_llm(
            system_prompt=_system_prompt(),
            user_prompt=_user_prompt(context),
            response_model=SprintRetro,
            gemini_schema=_GEMINI_SCHEMA,
        )
    except LLMCallFailed as exc:
        raise SprintRetroFailed(str(exc)) from exc
