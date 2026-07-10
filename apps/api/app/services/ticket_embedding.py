"""Ticket embeddings for semantic duplicate detection, via Gemini's
gemini-embedding-001 -- the only real embedding model available to this
app. See app/api/tickets.py's check_duplicates endpoint for the
synchronous similarity query this feeds, and worker/main.py for the
async job that generates and stores an embedding for every created (and
edited) ticket.

--- Why Gemini only, no Groq fallback -----------------------------

Checked empirically before choosing, not assumed: AsyncGroq's client
exposes an `embeddings` attribute, but that's inherited from the
OpenAI-compatible SDK base it's built on, not a real capability --
Groq's own model list (fetched live from this key) has no embedding
model at all, and calling `client.embeddings.create(...)` against it
returns a real 404, "model does not exist." Groq is a chat-completion
-only provider today. So unlike triage and ticket-parsing (both real
Groq-primary/Gemini-fallback cases), there is no second provider to
fall back to here -- if Gemini's embedding endpoint is down, duplicate
detection is genuinely unavailable, not silently degraded to some
assumed-equivalent path. This module is also deliberately not built on
app/services/llm_client.py: that module's whole shape (try provider A,
then B, validate a JSON response against a caller's Pydantic model)
doesn't fit an embedding call -- there's one provider, and the response
is a plain float vector, not JSON to validate against a schema. Reusing
that module here would be reuse for its own sake, not because the
shapes actually match.

--- Model and dimension choice: 768, not the model's own 3072 default -

gemini-embedding-001 is the only real embedding model on this key
(confirmed via a live models.list() call -- gemini-embedding-2-preview
and -2 are also listed, but 001 is the stable, non-preview choice).
Called with no dimension override it returns 3072 floats. It also
accepts an `output_dimensionality` override, using Matryoshka
Representation Learning (MRL) -- trained so a *leading* slice of the
full embedding stays independently useful, not an arbitrary truncation
that guts quality. Google's own docs single out 768 and 1536 as the
supported reduced sizes alongside the full 3072.

768 is used here for two concrete reasons: pgvector's ANN index types
(HNSW, IVFFlat) have a hard 2000-dimension ceiling -- a 3072-dim column
could never be indexed at all, only ever sequentially scanned, which
would foreclose that option permanently rather than just for now (see
migration 0019 for why no index is created yet regardless, at this
project's real scale). And smaller means less storage and a smaller
vector to compare per query, at a quality cost MRL is specifically
designed to keep small. Confirmed empirically that a real
output_dimensionality=768 call actually returns 768 floats and produces
sane, well-separated cosine distances (see
scripts/tune_duplicate_threshold.py's real-API run) -- not assumed from
Google's documentation alone.

Note on normalization: Google's docs warn that a *truncated* (non-3072)
embedding is not automatically re-normalized to unit length. That
doesn't matter here -- cosine similarity is scale-invariant by
definition (it divides by both vectors' magnitudes), and pgvector's
`<=>` operator computes true cosine distance, not raw dot product, so
an un-normalized magnitude never skews a comparison between two 768-dim
embeddings either way.
"""

import asyncio
import logging

from google import genai
from google.genai import types as genai_types

from app.core.config import settings

logger = logging.getLogger("services.ticket_embedding")

_EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMENSIONS = 768

# Same class of guardrail as llm_client.py's, applied to the one provider
# that exists here: don't let a hung embedding call hold a worker (or,
# for check_duplicates, an HTTP request a user is waiting on)
# indefinitely, and don't retry a genuinely broken call forever.
_CALL_TIMEOUT_SECONDS = 10.0
_MAX_ATTEMPTS = 2


class EmbeddingFailed(Exception):
    """Gemini's embedding endpoint was tried up to _MAX_ATTEMPTS times and
    never returned a usable vector. Terminal -- see worker/main.py (the
    ticket's embedding just stays NULL, logged, not retried forever) and
    app/api/tickets.py's check_duplicates (a 503, the same "real
    provider failure, not the input's fault" distinction already
    established for ticket parsing).
    """


def _embedding_text(title: str, description: str | None) -> str:
    return f"{title}\n\n{description}" if description else title


async def generate_embedding(title: str, description: str | None) -> list[float]:
    http_options = (
        genai_types.HttpOptions(base_url=settings.gemini_base_url) if settings.gemini_base_url else None
    )
    client = genai.Client(api_key=settings.gemini_api_key, http_options=http_options)
    text = _embedding_text(title, description)

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await asyncio.wait_for(
                client.aio.models.embed_content(
                    model=_EMBED_MODEL,
                    contents=text,
                    config=genai_types.EmbedContentConfig(output_dimensionality=EMBED_DIMENSIONS),
                ),
                timeout=_CALL_TIMEOUT_SECONDS,
            )
            values = list(response.embeddings[0].values) if response.embeddings else []
            if len(values) != EMBED_DIMENSIONS:
                raise ValueError(f"expected {EMBED_DIMENSIONS} dims, got {len(values)}")
        except Exception as exc:
            logger.warning(
                "embedding attempt %d/%d failed: %s: %s",
                attempt,
                _MAX_ATTEMPTS,
                type(exc).__name__,
                exc,
            )
            continue
        return values

    raise EmbeddingFailed(f"embedding generation exhausted its {_MAX_ATTEMPTS}-attempt budget")
