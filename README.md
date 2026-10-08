# llm-inference-platform

An inference gateway with the SLOs, admission control and cost accounting that
LLM serving actually needs — and a measured capacity model showing why.

Runs with **no GPU, no API key and no network**: the default backend is a
timing simulator calibrated to reproduce real prefill and batching behaviour.
Point it at a real model server with two environment variables when you want
to.

```
                    admission          decode slots (8)
                  ┌───────────┐        ┌──────────────┐
  client ───────▶ │  queue    │ ─────▶ │   backend    │ ──▶ SSE tokens
          429 ◀───│  (max 32) │        │ sim │ openai │
                  └───────────┘        └──────────────┘
                        │                     │
                   queue_wait            TTFT, ITL, tokens, cost
                        └──────────┬──────────┘
                                   ▼
                     Prometheus ─▶ SLO burn-rate alerts ─▶ Alertmanager
                                   └─▶ Grafana
```

## Quick start

```bash
make up                                  # whole stack
make bench                               # 16 concurrent for 60s
make sweep                               # capacity sweep (reproduces the docs)
make saturate                            # push past the knee, trip the alerts
make logs                                # watch alerts arrive
make down
```

- **Grafana** — http://localhost:3001 → *LLM Inference — SLOs, Throughput and Cost*
- **Prometheus alerts** — http://localhost:9091/alerts
- **Gateway** — http://localhost:8001/healthz

Ports are offset so this runs alongside the companion `sre-slo-platform` repo.

Against a real model instead of the simulator:

```bash
BACKEND=openai OPENAI_BASE_URL=http://host.docker.internal:11434 make up
```

## Why not just a request-duration histogram

Because for an LLM endpoint it measures the wrong thing:

- **Duration scales with output length.** A 2,000-token response takes ~35x a
  60-token one. A duration histogram across both measures the distribution of
  `max_tokens`, not service health.
- **Users perceive the first token.** 20 seconds of streaming that starts in
  200ms feels fast; 3 seconds that stalls silently for 2.8s feels broken. Total
  duration ranks those backwards.
- **Latency and throughput trade off directly.** Bigger batches raise tokens/sec
  and lower per-request speed. One number cannot express a tradeoff.

So the SLIs are **TTFT**, **inter-token latency** and **queue wait**, recorded
separately, plus token counters for spend.

## The three SLOs

| SLO | Target | Budget | Why that budget |
|---|---|---|---|
| **TTFT** | 95% of streaming requests get a first token within 500ms | 5% | A slow first token is a degraded promise, not a broken one |
| **Availability** | 99.9% of *accepted* requests succeed | 0.1% | A failed request is a broken promise |
| **Shed** | <1% of requests rejected with 429 | 1% | Capacity shortfall — different cause, different fix |

**429s get their own budget on purpose.** Counting them as availability
failures means one capacity event drains the wrong budget and the page says
"requests are failing" when it means "buy more GPUs". Ignoring them means the
gateway could shed 100% of traffic and still report perfect availability. Full
reasoning in [docs/inference-slos.md](docs/inference-slos.md).

## Measured capacity, not assumed

From `make sweep` — `DECODE_SLOTS=8`, 512-token prompts, 48 completion tokens:

| Concurrency | Throughput | TTFT p95 | ITL p95 | 429 | TTFT SLO |
|---|---|---|---|---|---|
| 1 | 40.9 tok/s | 142 ms | 37.0 ms | 0% | within |
| 4 | 147.2 tok/s | 140 ms | 40.3 ms | 0% | within |
| 8 | **259.3 tok/s** | 144 ms | 46.6 ms | 0% | within |
| 16 | 257.3 tok/s | 1,679 ms | 46.9 ms | 0% | **breached** |
| 32 | 258.2 tok/s | 4,658 ms | 46.9 ms | 0% | **breached** |
| 64 | 262.9 tok/s | 6,995 ms | 46.9 ms | 99.3% | **breached** |

**Throughput saturates at 8 and never improves — 2% more tokens/sec for 8x the
load, while TTFT p95 regresses 49x.** The knee sits exactly at `DECODE_SLOTS`.
Every request admitted past it buys nothing and costs latency, linearly. That
table is the entire argument for a bounded queue.

Note **ITL stays flat at ~47ms** from concurrency 8 up: inter-token latency is
set by effective batch size, which the semaphore caps. All the degradation
moves into TTFT. A single "latency" metric would have averaged two unrelated
effects into one confusing line.

The queue-sizing maths (Little's Law, and why the configured depth of 32 is
~10x too generous for a 500ms target) is in
[docs/capacity-model.md](docs/capacity-model.md).

## Three bugs found by running it

All three were surfaced by testing, not by reading config, and all three are
documented in place rather than quietly patched:

**1. A healthy service reported NO DATA for availability.**
`llm_requests_total{outcome="error"}` does not exist until the first error, and
dividing an empty vector by anything yields an empty vector. So zero errors
produced *no SLI value at all* — indistinguishable from a broken exporter, and
it would have tripped the `absent()` meta-alert on a perfectly working system.
Fixed with `or vector(0)`, and CI now asserts the SLI returns a value.

**2. Multi-process metrics corrupt every counter.** `prometheus_client` keeps
counters in process memory, so more than one uvicorn worker makes Prometheus
scrape alternating registries and read the drops as counter resets. Here it is
doubly wrong: the admission semaphore must be shared to be admission control
at all. Pinned to one worker, with the reasoning in the Dockerfile.

**3. Histogram buckets saturate under overload.** Queue wait tops out at a 5s
finite bucket, so during the concurrency-64 step `histogram_quantile` clamped
there while real waits were higher. The reported p95 is a floor, not a
measurement. Left as-is (anything past 10s is equally bad) but documented, so
nobody trusts the number later.

## Layout

```
gateway/
  app.py            admission control, queueing, SSE streaming, measurement
  backends.py       simulated timing model + OpenAI-compatible passthrough
  metrics.py        TTFT, ITL, queue wait, tokens, cost, concurrency
observability/
  prometheus/rules/ SLI recording rules + burn-rate and capacity alerts
  grafana/          provisioned SLO / throughput / cost dashboard
  alertmanager/     page vs ticket routing, inhibition
bench/
  loadgen.py        token-aware load generator (client-side TTFT and ITL)
  sweep.sh          capacity sweep
docs/
  inference-slos.md why these SLIs, why these budgets, why 429 is separate
  capacity-model.md the measured curve, Little's Law, queue sizing
  runbook-ttft.md   saturation vs backend attribution, and what to do
.github/workflows/  promtool validation, ruff, and a smoke test that streams
                    tokens and asserts every SLI returns a value
```

## Honest limitations

- The simulator reproduces prefill scaling, batch-size decode penalty and
  jittery token timing. It does **not** model KV-cache eviction,
  paged-attention memory pressure, speculative decoding or GPU throttling. The
  capacity *shape* transfers; the absolute tok/s numbers are a property of
  this laptop.
- The 99.3% rejection rate at concurrency 64 is inflated by the open-loop load
  generator — rejections return instantly, so workers spin. The meaningful
  signal is *that* shedding engaged and at which concurrency, not the ratio.
- Single replica, no multi-replica routing or per-tenant fairness. The SLIs
  aggregate correctly across replicas by construction (counter ratios, not
  quantiles), but that path is untested here.
- No minimum-traffic guard on the ratio SLIs; at low QPS a single error is a
  large ratio. See the discussion in `docs/inference-slos.md`.
