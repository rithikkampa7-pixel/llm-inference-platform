"""Token-aware load generator.

Standard HTTP load tools report request duration, which for a streaming LLM
endpoint is dominated by how many tokens were requested and tells you almost
nothing. This measures what matters, client-side:

  TTFT  time from send to the first SSE token
  ITL   gaps between subsequent tokens
  shed  fraction of requests rejected with 429

Measuring client-side matters: it independently checks the gateway's own
metrics. If server TTFT looks healthy and client TTFT does not, the gap is in
the network, the proxy, or the measurement itself.

Usage:
  python bench/loadgen.py --concurrency 16 --duration 60
  python bench/loadgen.py --concurrency 64 --duration 60 --prompt-tokens 4096
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


async def one_request(
    client: httpx.AsyncClient, url: str, prompt_tokens: int, max_tokens: int
) -> dict:
    body = {
        "prompt_tokens": prompt_tokens,
        "max_tokens": max_tokens,
        "stream": True,
    }
    started = time.perf_counter()
    ttft: float | None = None
    gaps: list[float] = []
    tokens = 0
    last = started

    try:
        async with client.stream("POST", url, json=body) as resp:
            if resp.status_code == 429:
                return {"outcome": "rejected"}
            if resp.status_code >= 500:
                return {"outcome": "error"}
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                if line[6:].strip() == "[DONE]":
                    break
                now = time.perf_counter()
                if ttft is None:
                    ttft = now - started
                else:
                    gaps.append(now - last)
                last = now
                tokens += 1
    except Exception as exc:  # noqa: BLE001 - any transport failure counts
        return {"outcome": "error", "detail": repr(exc)}

    return {
        "outcome": "ok",
        "ttft": ttft,
        "itl": gaps,
        "tokens": tokens,
        "total": time.perf_counter() - started,
    }


async def worker(stop_at: float, results: list[dict], **kw) -> None:
    # One client per worker: sharing a single client across many coroutines
    # serialises on its connection pool and would measure the pool, not the
    # server.
    async with httpx.AsyncClient(timeout=120.0) as client:
        while time.perf_counter() < stop_at:
            results.append(await one_request(client, **kw))


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(int(round(p / 100 * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[idx]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8001/v1/chat/completions")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--prompt-tokens", type=int, default=512)
    ap.add_argument("--max-tokens", type=int, default=64)
    args = ap.parse_args()

    results: list[dict] = []
    stop_at = time.perf_counter() + args.duration
    wall_start = time.perf_counter()

    print(
        f"-> {args.concurrency} concurrent, {args.duration}s, "
        f"prompt={args.prompt_tokens} max_tokens={args.max_tokens}"
    )
    await asyncio.gather(*[
        worker(stop_at, results, url=args.url,
               prompt_tokens=args.prompt_tokens, max_tokens=args.max_tokens)
        for _ in range(args.concurrency)
    ])
    wall = time.perf_counter() - wall_start

    ok = [r for r in results if r["outcome"] == "ok"]
    rejected = sum(1 for r in results if r["outcome"] == "rejected")
    errors = sum(1 for r in results if r["outcome"] == "error")
    ttfts = [r["ttft"] for r in ok if r.get("ttft") is not None]
    itls = [g for r in ok for g in r.get("itl", [])]
    total_tokens = sum(r.get("tokens", 0) for r in ok)

    print(f"\n{'requests':<22}{len(results)}")
    print(f"{'  ok':<22}{len(ok)}")
    print(f"{'  rejected (429)':<22}{rejected}"
          f"  ({rejected / max(len(results), 1):.2%})")
    print(f"{'  errors':<22}{errors}")
    print(f"\n{'throughput':<22}{total_tokens / wall:.1f} completion tok/s")
    print(f"{'request rate':<22}{len(results) / wall:.1f} req/s")

    if ttfts:
        print(f"\n{'TTFT p50':<22}{pct(ttfts, 50) * 1000:.0f} ms")
        print(f"{'TTFT p95':<22}{pct(ttfts, 95) * 1000:.0f} ms")
        print(f"{'TTFT p99':<22}{pct(ttfts, 99) * 1000:.0f} ms")
        breached = sum(1 for t in ttfts if t > 0.5) / len(ttfts)
        verdict = "WITHIN" if breached <= 0.05 else "BREACHED"
        print(f"\n{'TTFT SLO (p95<500ms)':<22}{breached:.2%} slow -> {verdict}")

    if itls:
        print(f"\n{'ITL p50':<22}{statistics.median(itls) * 1000:.1f} ms")
        print(f"{'ITL p95':<22}{pct(itls, 95) * 1000:.1f} ms")


if __name__ == "__main__":
    asyncio.run(main())
