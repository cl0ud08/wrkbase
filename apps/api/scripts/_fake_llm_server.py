"""A local stand-in for Groq and Gemini, speaking just enough of each
provider's real wire shape to satisfy their official SDKs' response
parsing -- used only so verify_triage.py (CI-wired, runs on every push)
can exercise the *real* worker code path end to end (queue consume,
prompt building, HTTP call via the real SDKs, response parsing, Pydantic
validation, DB write, Redis publish) without paying for or depending on
the real vendor APIs. See verify_triage.py's module docstring for the
full reasoning on why this exists instead of either "call the real APIs
in CI" or "mock triage_ticket() at the Python level."

Both AsyncGroq and genai.Client accept a plain base-URL override natively
(this is standard SDK configuration, not a hook added for testing) --
GROQ_BASE_URL/GEMINI_BASE_URL point them here during the CI-only run.

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

# A fixed, deterministic result -- this exists to prove the plumbing
# works, not to exercise prompt/model quality (that's verify_triage_llm.py,
# against the real APIs, run manually). Every request gets the same
# answer; verify_triage.py only asserts the shape it's serialized into,
# same as the hardcoded placeholder proof-script assertions did before
# this file was needed.
_FAKE_RESULT = {"priority": "medium", "labels": ["stub"], "reasoning": "Deterministic fake response."}


@app.post("/openai/v1/chat/completions")
async def groq_chat_completions(request: Request) -> JSONResponse:
    # The minimum subset of Groq's real response envelope AsyncGroq's own
    # parsing actually reads: choices[0].message.content. Everything else
    # a real response includes (usage stats, ids, etc.) is omitted --
    # this only needs to satisfy this app's own code, not replicate Groq.
    return JSONResponse(
        {
            "id": "fake-completion",
            "object": "chat.completion",
            "model": "llama-3.3-70b-versatile",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(_FAKE_RESULT)},
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
    return JSONResponse(
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": json.dumps(_FAKE_RESULT)}], "role": "model"},
                    "finishReason": "STOP",
                    "index": 0,
                }
            ]
        }
    )


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9100
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
