"""Shared Groq-primary/Gemini-fallback calling core.

Extracted from llm_triage.py (Phase 3 slice 2) once ticket parsing (slice
3) became a second real call site needing the exact same mechanics --
timeout, retry budget, provider fallback, structured-JSON parsing into a
caller-supplied Pydantic model. Two real call sites is what justifies this
abstraction; a single one wouldn't have.

This module has no opinion about *what* is being asked of the model --
that's entirely the caller's system/user prompt and response schema. It
only owns the parts that would otherwise be duplicated byte-for-byte
between llm_triage.py and ticket_parse.py: which provider goes first, how
many times each gets tried, how long a single call is allowed to hang,
and validating the raw response through the caller's model before trusting
it. See llm_triage.py's module docstring for the empirically-verified
reasoning behind json_object mode (Groq) and response_schema (Gemini) --
that reasoning doesn't change here, it just isn't re-derived per call site.
"""

import asyncio
import logging
from typing import TypeVar

from google import genai
from google.genai import types as genai_types
from groq import AsyncGroq
from pydantic import BaseModel

from app.core.config import settings

logger = logging.getLogger("services.llm_client")

_GROQ_MODEL = "llama-3.3-70b-versatile"
_GEMINI_MODEL = "gemini-2.5-flash"

# Don't let a hung provider call hold the caller (a worker's RabbitMQ
# message, or a synchronous HTTP request a user is actively waiting on)
# indefinitely.
_CALL_TIMEOUT_SECONDS = 10.0

# Bounds the *total* number of real LLM calls one call_llm() invocation can
# ever cost: at most this many tries against Groq, then at most this many
# against Gemini, then LLMCallFailed. See each caller's own module for what
# it does with that terminal failure -- this module has no opinion on that
# either.
_MAX_ATTEMPTS_PER_PROVIDER = 2

TModel = TypeVar("TModel", bound=BaseModel)


class LLMCallFailed(Exception):
    """Both providers were tried, up to their retry budgets, and neither
    produced a response that parsed into the caller's response model. A
    malformed-but-parseable response counts as a failed attempt here too,
    exactly like a timeout or a rate limit -- never a result written
    half-validated. Callers translate this into their own domain-specific
    terminal-failure behavior (see llm_triage.TriageFailed and
    ticket_parse.TicketParseUnavailable); this module has no opinion about
    what that behavior should be.
    """


async def _call_groq(system_prompt: str, user_prompt: str, response_model: type[TModel]) -> TModel:
    client = AsyncGroq(api_key=settings.groq_api_key, base_url=settings.groq_base_url)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=_GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        ),
        timeout=_CALL_TIMEOUT_SECONDS,
    )
    content = response.choices[0].message.content
    if content is None:
        raise ValueError("Groq returned an empty completion")
    return response_model.model_validate_json(content)


async def _call_gemini(
    system_prompt: str, user_prompt: str, response_model: type[TModel], gemini_schema: dict
) -> TModel:
    http_options = (
        genai_types.HttpOptions(base_url=settings.gemini_base_url) if settings.gemini_base_url else None
    )
    client = genai.Client(api_key=settings.gemini_api_key, http_options=http_options)
    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=_GEMINI_MODEL,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=gemini_schema,
                temperature=0.2,
            ),
        ),
        timeout=_CALL_TIMEOUT_SECONDS,
    )
    if not response.text:
        raise ValueError("Gemini returned an empty response")
    return response_model.model_validate_json(response.text)


async def call_llm(
    *,
    system_prompt: str,
    user_prompt: str,
    response_model: type[TModel],
    gemini_schema: dict,
) -> tuple[TModel, str]:
    """Returns (result, provider_name). gemini_schema is the caller's own
    Gemini response_schema dict (OpenAPI-3.0-subset shape, e.g.
    {"type": "OBJECT", "properties": {...}, "required": [...]}) -- not
    derived automatically from response_model, since Gemini's schema
    format isn't quite standard JSON Schema (enum values, type names in
    caps) and getting that translation subtly wrong for every future
    caller silently is a worse failure mode than each caller spelling out
    its own schema explicitly, the same way this project's migrations are
    hand-written rather than autogenerated.
    """
    for provider_name, call in (
        ("groq", lambda: _call_groq(system_prompt, user_prompt, response_model)),
        ("gemini", lambda: _call_gemini(system_prompt, user_prompt, response_model, gemini_schema)),
    ):
        for attempt in range(1, _MAX_ATTEMPTS_PER_PROVIDER + 1):
            try:
                result = await call()
            except Exception as exc:
                logger.warning(
                    "%s call attempt %d/%d failed: %s: %s",
                    provider_name,
                    attempt,
                    _MAX_ATTEMPTS_PER_PROVIDER,
                    type(exc).__name__,
                    exc,
                )
                continue
            return result, provider_name

    raise LLMCallFailed(f"both providers exhausted their {_MAX_ATTEMPTS_PER_PROVIDER}-attempt budget")
