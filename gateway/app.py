"""Inference gateway: admission control, queueing, streaming, measurement.

The gateway owns three things the model server does not:

  Admission  -- a bounded queue. Beyond it, shed load with 429 rather than
                accepting work that will time out anyway. An unbounded queue
                converts a capacity problem into a latency problem and then
                into a total outage.
  Fairness   -- a fixed number of decode slots, so one burst cannot starve
                everything else.
  Measurement -- TTFT, inter-token latency and queue wait, recorded
                separately so a latency incident can be attributed to
                saturation or to the backend.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from backends import GenerationRequest, build_backend
from metrics import (
    ACTIVE,
    CAPACITY,
    COST,
    ITL,
    QUEUE_DEPTH,
    QUEUE_WAIT,
    REQUESTS,
    TOKENS,
    TTFT,
)

MODEL = os.getenv("MODEL_NAME", "sim-7b-instruct")
DECODE_SLOTS = int(os.getenv("DECODE_SLOTS", "8"))
MAX_QUEUE = int(os.getenv("MAX_QUEUE_DEPTH", "32"))

# Priced per million tokens, the unit every provider publishes.
PROMPT_USD_PER_M = float(os.getenv("PROMPT_USD_PER_M", "0.50"))
COMPLETION_USD_PER_M = float(os.getenv("COMPLETION_USD_PER_M", "1.50"))

backend = build_backend()
slots = asyncio.Semaphore(DECODE_SLOTS)

# Tracks requests admitted but not yet decoding. Distinct from the semaphore's
# internal counter because we need it as a gauge for capacity alerting.
_queued = 0
_active = 0
_lock = asyncio.Lock()


class ChatRequest(BaseModel):
    model: str | None = None
    prompt_tokens: int = Field(default=512, ge=1, le=131072)
    max_tokens: int = Field(default=128, ge=1, le=4096)
    stream: bool = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    CAPACITY.labels(MODEL).set(DECODE_SLOTS)
    QUEUE_DEPTH.labels(MODEL).set(0)
    ACTIVE.labels(MODEL).set(0)
    yield


app = FastAPI(title="llm-inference-gateway", lifespan=lifespan)


@asynccontextmanager
async def decode_slot() -> AsyncIterator[tuple[float, int]]:
    """Queue for a decode slot. Yields (queue_wait_seconds, batch_size)."""
    global _queued, _active
    async with _lock:
        _queued += 1
        QUEUE_DEPTH.labels(MODEL).set(_queued)

    waited_from = time.perf_counter()
    try:
        await slots.acquire()
    except asyncio.CancelledError:
        async with _lock:
            _queued -= 1
            QUEUE_DEPTH.labels(MODEL).set(_queued)
        raise

    wait_s = time.perf_counter() - waited_from
    QUEUE_WAIT.labels(MODEL).observe(wait_s)

    async with _lock:
        _queued -= 1
        _active += 1
        QUEUE_DEPTH.labels(MODEL).set(_queued)
        ACTIVE.labels(MODEL).set(_active)
        batch = _active

    try:
        yield wait_s, batch
    finally:
        async with _lock:
            _active -= 1
            ACTIVE.labels(MODEL).set(_active)
        slots.release()


def _charge(prompt_tokens: int, completion_tokens: int) -> None:
    TOKENS.labels(MODEL, "prompt").inc(prompt_tokens)
    TOKENS.labels(MODEL, "completion").inc(completion_tokens)
    COST.labels(MODEL).inc(
        prompt_tokens / 1_000_000 * PROMPT_USD_PER_M
        + completion_tokens / 1_000_000 * COMPLETION_USD_PER_M
    )


@app.post("/v1/chat/completions")
async def chat(req: ChatRequest, request: Request):
    # Admission control. Checked before queueing, so a rejection is cheap and
    # the client learns immediately instead of timing out in a queue.
    if _queued >= MAX_QUEUE:
        REQUESTS.labels(MODEL, "rejected").inc()
        return JSONResponse(
            {"error": {"message": "queue full, retry later", "type": "capacity"}},
            status_code=429,
            headers={"Retry-After": "1"},
        )

    model = req.model or MODEL
    gen_req = GenerationRequest(
        model=model, prompt_tokens=req.prompt_tokens, max_tokens=req.max_tokens
    )

    # TTFT is measured from request arrival, deliberately including queue
    # wait. That is what the user experiences; excluding it would let the
    # gateway hide its own saturation behind a healthy-looking metric.
    arrived = time.perf_counter()

    if not req.stream:
        return await _complete_blocking(gen_req, arrived)

    async def body() -> AsyncIterator[bytes]:
        emitted = 0
        first_seen = False
        last_token_at = 0.0
        try:
            async with decode_slot() as (_wait, batch):
                async for token in backend.generate(gen_req, batch):
                    now = time.perf_counter()
                    if not first_seen:
                        TTFT.labels(model, "true").observe(now - arrived)
                        first_seen = True
                    else:
                        ITL.labels(model).observe(now - last_token_at)
                    last_token_at = now
                    emitted += 1
                    yield f"data: {token}\n\n".encode()
            yield b"data: [DONE]\n\n"
            REQUESTS.labels(MODEL, "ok").inc()
        except asyncio.CancelledError:
            # The client hung up mid-stream. Common, and not an error -- but
            # it must not be counted as success or the SLI overstates health.
            REQUESTS.labels(MODEL, "cancelled").inc()
            raise
        except Exception:
            REQUESTS.labels(MODEL, "error").inc()
            yield b'data: {"error":"generation failed"}\n\n'
        finally:
            _charge(gen_req.prompt_tokens, emitted)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _complete_blocking(gen_req: GenerationRequest, arrived: float):
    chunks: list[str] = []
    try:
        async with decode_slot() as (_wait, batch):
            async for token in backend.generate(gen_req, batch):
                if not chunks:
                    # For a non-streaming request TTFT is not user-visible,
                    # but recording it keeps backend health comparable across
                    # both modes.
                    TTFT.labels(gen_req.model, "false").observe(
                        time.perf_counter() - arrived
                    )
                chunks.append(token)
    except Exception:
        REQUESTS.labels(MODEL, "error").inc()
        return JSONResponse(
            {"error": {"message": "generation failed"}}, status_code=500
        )

    REQUESTS.labels(MODEL, "ok").inc()
    _charge(gen_req.prompt_tokens, len(chunks))
    return {
        "model": gen_req.model,
        "choices": [{"message": {"role": "assistant", "content": "".join(chunks)}}],
        "usage": {
            "prompt_tokens": gen_req.prompt_tokens,
            "completion_tokens": len(chunks),
            "total_tokens": gen_req.prompt_tokens + len(chunks),
        },
    }


@app.get("/healthz")
async def healthz():
    # Uninstrumented on purpose: probes must never enter the SLI.
    return {"status": "ok", "slots": DECODE_SLOTS, "queued": _queued}


@app.get("/metrics")
async def metrics():
    from fastapi.responses import Response

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
