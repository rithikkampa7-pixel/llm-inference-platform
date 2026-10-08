"""Pluggable inference backends.

Two implementations:

  simulated  -- the default. Generates tokens with realistic timing and no
                GPU, API key or network. This makes the repo runnable and its
                load tests reproducible by anyone, which a real backend would
                not be.
  openai     -- passthrough to any OpenAI-compatible server (vLLM, Ollama,
                TGI, LM Studio). Use this to point the same gateway and the
                same dashboards at a real model.

The simulator is explicitly a *model of* inference timing, not inference. It
is calibrated to reproduce three behaviours that matter operationally:

  1. Prefill cost scales with prompt length, so TTFT grows with the prompt.
  2. Decode speed per request degrades as batch size grows -- the central
     latency/throughput tradeoff in LLM serving.
  3. Token timing is jittery, not uniform, so percentiles are meaningful.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass
class GenerationRequest:
    model: str
    prompt_tokens: int
    max_tokens: int


class Backend(Protocol):
    async def generate(
        self, req: GenerationRequest, batch_size: int
    ) -> AsyncIterator[str]:
        """Yield tokens one at a time."""
        ...


class SimulatedBackend:
    """Timing model of a transformer served with continuous batching."""

    def __init__(
        self,
        prefill_tokens_per_sec: float = 9000.0,
        decode_tokens_per_sec: float = 55.0,
        batch_penalty: float = 0.06,
        base_overhead_s: float = 0.025,
    ) -> None:
        self.prefill_tps = prefill_tokens_per_sec
        self.decode_tps = decode_tokens_per_sec
        # Each additional concurrent request slows every request's decode by
        # roughly this fraction. Real systems are not linear, but linear is
        # the right first-order model and makes the tradeoff legible.
        self.batch_penalty = batch_penalty
        self.base_overhead = base_overhead_s

    def _decode_interval(self, batch_size: int) -> float:
        effective_tps = self.decode_tps / (
            1.0 + self.batch_penalty * max(0, batch_size - 1)
        )
        return 1.0 / max(effective_tps, 1.0)

    async def generate(
        self, req: GenerationRequest, batch_size: int
    ) -> AsyncIterator[str]:
        # Prefill: one pass over the prompt, compute-bound and roughly linear
        # in prompt length. This is why long prompts hurt TTFT specifically.
        prefill_s = self.base_overhead + req.prompt_tokens / self.prefill_tps
        await asyncio.sleep(prefill_s * random.uniform(0.85, 1.25))

        interval = self._decode_interval(batch_size)
        for i in range(req.max_tokens):
            # Lognormal-ish jitter: mostly steady with occasional long gaps,
            # which is what real decode loops look like under contention.
            jitter = random.lognormvariate(0.0, 0.35)
            await asyncio.sleep(min(interval * jitter, interval * 6))
            yield f"tok{i} "


class OpenAICompatibleBackend:
    """Streams from any OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def generate(
        self, req: GenerationRequest, batch_size: int
    ) -> AsyncIterator[str]:
        import json

        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        body = {
            "model": req.model,
            "messages": [{"role": "user", "content": "x" * req.prompt_tokens}],
            "max_tokens": req.max_tokens,
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                json=body,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk["choices"][0].get("delta", {})
                    if content := delta.get("content"):
                        yield content


def build_backend() -> Backend:
    kind = os.getenv("BACKEND", "simulated").lower()
    if kind == "openai":
        base = os.getenv("OPENAI_BASE_URL")
        if not base:
            raise RuntimeError("BACKEND=openai requires OPENAI_BASE_URL")
        return OpenAICompatibleBackend(base, os.getenv("OPENAI_API_KEY"))
    return SimulatedBackend(
        decode_tokens_per_sec=float(os.getenv("SIM_DECODE_TPS", "55")),
        batch_penalty=float(os.getenv("SIM_BATCH_PENALTY", "0.06")),
    )
