"""A local stand-in for Groq and Gemini, speaking just enough of each
provider's real wire shape to satisfy their official SDKs' response
parsing -- used so verify_triage.py, verify_ticket_parse.py,
verify_duplicate_detection.py, and verify_appsec_triggers.py (all
CI-wired, run on every push) can exercise the *real* code paths end to
end (queue consume or HTTP request, prompt building, HTTP call via the
real SDKs, response parsing, Pydantic validation, DB write/API response)
without paying for or depending on the real vendor APIs. See
verify_triage.py's module docstring for the full reasoning on why this
exists instead of either "call the real APIs in CI" or "mock the service
functions at the Python level."

Both AsyncGroq and genai.Client accept a plain base-URL override natively
(this is standard SDK configuration, not a hook added for testing) --
GROQ_BASE_URL/GEMINI_BASE_URL point them here during the CI-only run.

Content-aware, not just a single fixed reply: llm_triage.py and
ticket_parse.py send genuinely different-shaped prompts and expect
genuinely different-shaped JSON back, and verify_ticket_parse.py needs to
exercise both a confident and a low-confidence parse. Rather than a
special test-only signal, this reads the same prompt text a real model
would read to decide its answer -- distinguishing triage vs. parse by
each one's own system prompt wording, and confident vs. low-confidence
within parse by a plainly-named sentinel
("stub_trigger_low_confidence") the proof script puts in its own
deliberately-ambiguous test input, visible in both places, not hidden.

Embeddings work the same way, but can't be content-aware about
*meaning* the way a real embedding model is -- there's no real semantic
understanding here to approximate cheaply. Instead, a small set of
plainly-named sentinels map deterministically to a small set of fixed
vectors, geometrically constructed so their cosine similarities are
exactly known in advance (see _VECTOR_BASE/_VECTOR_NEAR/_VECTOR_FAR
below): similarity(BASE, NEAR) = 0.9 (above SIMILARITY_THRESHOLD, a
real match), similarity(BASE, FAR) = similarity(NEAR, FAR) = well below
it (never a match). This tests the actual plumbing this app's own code
is responsible for -- the pgvector query, its project/org scoping, the
threshold comparison, re-embedding on edit -- deterministically and for
free. It does NOT test whether Gemini's real embeddings actually
capture ticket-text semantics well enough for SIMILARITY_THRESHOLD to
mean anything in practice -- that's what
scripts/tune_duplicate_threshold.py is for, against the real API,
run manually.

AppSec review calls are chat completions, the same wire shape as
triage/parse, so they need no new route -- only a third branch in
_fake_result_for, distinguished the same content-aware way as triage
vs. parse ("application security reviewer" is unique to
appsec_review.py's own system prompt). Which categories the real call
asked about needs no sentinel either, unlike parse's confident/
low-confidence split: appsec_review.py's prompt embeds each matched
category as "### Label (key)", so this stub just reads the same key
back out of its own prompt text and echoes it into
categories_addressed -- content the request itself already contains,
not a hidden test-only signal.

Run: python -m scripts._fake_llm_server [port]
"""

import json
import math
import sys

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()


@app.get("/health")
async def health() -> dict[str, str]:
    # Same readiness-polling convention every other background process in
    # CI already uses (see ci.yml's uvicorn-startup loops) -- lets the
    # workflow curl this instead of guessing a fixed sleep duration.
    return {"status": "ok"}


_FAKE_TRIAGE_RESULT = {
    "priority": "medium",
    "labels": ["stub"],
    "reasoning": "Deterministic fake response.",
}

_FAKE_PARSE_CONFIDENT = {
    "confident": True,
    "title": "Stub parsed ticket",
    "description": "Deterministic fake parse response.",
    "type": "task",
    "priority": "medium",
    "labels": ["stub"],
    "clarification": None,
}

_FAKE_PARSE_LOW_CONFIDENCE = {
    "confident": False,
    "title": None,
    "description": None,
    "type": None,
    "priority": None,
    "labels": [],
    "clarification": "Stub: deliberately ambiguous test input, nothing to confidently extract.",
}

_LOW_CONFIDENCE_SENTINEL = "stub_trigger_low_confidence"

# The category keys appsec_triggers.py actually defines -- appsec_review.py's
# system prompt embeds each matched category as "### Label (key)", so
# searching for "(key)" in the prompt text tells this stub exactly which
# categories the real call asked about, with no sentinel needed: unlike
# parse's confident/low-confidence split (a genuinely different semantic
# outcome the stub can't derive from the input alone), which categories
# were requested is already fully determined by the prompt's own content.
_APPSEC_CATEGORY_KEYS = (
    "file_upload", "auth_permission", "payment_pii", "external_api", "admin_permission",
)


def _prompt_text(body: dict) -> str:
    # Groq/OpenAI-compatible shape: {"messages": [{"role": ..., "content": ...}, ...]}.
    # Gemini's real REST shape (confirmed empirically, not assumed --
    # see the CHANGELOG entry this stub was extended under):
    # {"contents": [{"parts": [{"text": ...}], ...}], "systemInstruction": {"parts": [{"text": ...}]}}.
    parts = [m.get("content", "") for m in body.get("messages", [])]
    for part in body.get("systemInstruction", {}).get("parts", []):
        parts.append(part.get("text", ""))
    for content in body.get("contents", []):
        for part in content.get("parts", []):
            parts.append(part.get("text", ""))
    return " ".join(parts).lower()


def _fake_appsec_review_for(text: str) -> dict:
    matched = [key for key in _APPSEC_CATEGORY_KEYS if f"({key})" in text]
    return {
        "comment": f"Stub deterministic security review covering: {', '.join(matched) or 'none'}.",
        "categories_addressed": matched,
    }


def _fake_result_for(body: dict) -> dict:
    text = _prompt_text(body)
    if "application security reviewer" in text:
        return _fake_appsec_review_for(text)
    if "ticket-parsing assistant" not in text:
        return _FAKE_TRIAGE_RESULT
    if _LOW_CONFIDENCE_SENTINEL in text:
        return _FAKE_PARSE_LOW_CONFIDENCE
    return _FAKE_PARSE_CONFIDENT


# --- Deterministic embedding vectors ------------------------------------
#
# Two orthonormal basis vectors in 768-dim space (E1: first half all
# equal, second half zero; E2: the reverse), so any vector built as
# cos(theta)*E1 + sin(theta)*E2 is automatically unit length and has a
# cosine similarity to E1 of exactly cos(theta) -- exact, provable
# control over the similarity scripts/verify_duplicate_detection.py
# needs, not an approximation.
_DIM = 768
_HALF = _DIM // 2


def _e1() -> list[float]:
    return [1.0 / math.sqrt(_HALF)] * _HALF + [0.0] * (_DIM - _HALF)


def _e2() -> list[float]:
    return [0.0] * _HALF + [1.0 / math.sqrt(_DIM - _HALF)] * (_DIM - _HALF)


def _mix(theta: float) -> list[float]:
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return [cos_t * a + sin_t * b for a, b in zip(_e1(), _e2())]


_VECTOR_BASE = _e1()  # similarity(BASE, BASE) = 1.0
_VECTOR_NEAR = _mix(math.acos(0.9))  # similarity(BASE, NEAR) = 0.9 -- above SIMILARITY_THRESHOLD
_VECTOR_FAR = _e2()  # similarity(BASE, FAR) = 0.0, similarity(NEAR, FAR) = ~0.436 -- both below it

_EMBED_MARKERS = {
    "stub_embed_near": _VECTOR_NEAR,
    "stub_embed_far": _VECTOR_FAR,
}


def _fake_embedding_for(body: dict) -> list[float]:
    # Real request shape (confirmed empirically): {"requests": [{"content":
    # {"parts": [{"text": ...}]}, ...}]} -- batchEmbedContents always
    # wraps even a single embed_content() call in a "requests" list.
    parts: list[str] = []
    for req in body.get("requests", []):
        for part in req.get("content", {}).get("parts", []):
            parts.append(part.get("text", ""))
    text = " ".join(parts).lower()

    for marker, vector in _EMBED_MARKERS.items():
        if marker in text:
            return vector
    return _VECTOR_BASE


@app.post("/openai/v1/chat/completions")
async def groq_chat_completions(request: Request) -> JSONResponse:
    # The minimum subset of Groq's real response envelope AsyncGroq's own
    # parsing actually reads: choices[0].message.content. Everything else
    # a real response includes (usage stats, ids, etc.) is omitted --
    # this only needs to satisfy this app's own code, not replicate Groq.
    result = _fake_result_for(await request.json())
    return JSONResponse(
        {
            "id": "fake-completion",
            "object": "chat.completion",
            "model": "llama-3.3-70b-versatile",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(result)},
                    "finish_reason": "stop",
                }
            ],
        }
    )


@app.post("/v1beta/models/{model_and_method:path}")
async def gemini_models_endpoint(model_and_method: str, request: Request) -> JSONResponse:
    body = await request.json()

    if model_and_method.endswith(":batchEmbedContents"):
        # The minimum subset genai's own response.embeddings[0].values
        # parsing actually reads.
        return JSONResponse({"embeddings": [{"values": _fake_embedding_for(body)}]})

    # generateContent (chat) -- same minimum-subset idea: candidates[0]
    # .content.parts[0].text is what genai's response.text property
    # actually reads under the hood.
    result = _fake_result_for(body)
    return JSONResponse(
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": json.dumps(result)}], "role": "model"},
                    "finishReason": "STOP",
                    "index": 0,
                }
            ]
        }
    )


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9100
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
