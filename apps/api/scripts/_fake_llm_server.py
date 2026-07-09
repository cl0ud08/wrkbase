"""A local stand-in for Groq and Gemini, speaking just enough of each
provider's real wire shape to satisfy their official SDKs' response
parsing -- used so verify_triage.py and verify_ticket_parse.py (both
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

Run: python -m scripts._fake_llm_server [port]
"""

import json
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


def _fake_result_for(body: dict) -> dict:
    text = _prompt_text(body)
    if "ticket-parsing assistant" not in text:
        return _FAKE_TRIAGE_RESULT
    if _LOW_CONFIDENCE_SENTINEL in text:
        return _FAKE_PARSE_LOW_CONFIDENCE
    return _FAKE_PARSE_CONFIDENT


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
async def gemini_generate_content(model_and_method: str, request: Request) -> JSONResponse:
    # Same minimum-subset idea for Gemini's shape: candidates[0].content
    # .parts[0].text is what genai's response.text property actually
    # reads under the hood.
    result = _fake_result_for(await request.json())
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
