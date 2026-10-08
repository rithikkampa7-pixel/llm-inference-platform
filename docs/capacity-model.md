# Capacity model — measured, not assumed

All numbers below came from `bench/sweep.sh` against the simulated backend on
one laptop: `DECODE_SLOTS=8`, `MAX_QUEUE_DEPTH=32`, 512-token prompts,
48 completion tokens, 20s per step. Reproduce with:

```bash
make up
make sweep
```

## The curve

| Concurrency | Throughput (completion tok/s) | TTFT p95 | TTFT p99 | ITL p95 | 429 rate | TTFT SLO |
|---|---|---|---|---|---|---|
| 1 | 40.9 | 142 ms | 142 ms | 37.0 ms | 0% | within |
| 4 | 147.2 | 140 ms | 147 ms | 40.3 ms | 0% | within |
| 8 | **259.3** | 144 ms | 164 ms | 46.6 ms | 0% | within |
| 16 | 257.3 | 1,679 ms | 1,744 ms | 46.9 ms | 0% | **breached** |
| 32 | 258.2 | 4,658 ms | 4,669 ms | 46.9 ms | 0% | **breached** |
| 64 | 262.9 | 6,995 ms | 7,314 ms | 46.9 ms | 99.3% | **breached** |

## Reading it

**Throughput saturates at concurrency 8 and never improves again.** 259 tok/s
at 8, 263 tok/s at 64 — a 2% gain for 8x the offered load. Meanwhile TTFT p95
goes from 144ms to 6,995ms, a **49x regression**. Every request admitted
beyond slot capacity buys no throughput and costs latency, linearly.

That is the whole argument for a bounded queue in one table. The knee is at
`DECODE_SLOTS`, exactly where it should be: eight slots, eight concurrent
decodes, and past that work simply waits.

**ITL is flat at ~47ms from concurrency 8 upward.** Inter-token latency is
governed by the *effective batch size*, which the semaphore caps at 8. So once
the gateway is saturated, extra load does not make the stream stutter more —
it makes you wait longer to start. The degradation moves entirely into TTFT,
which is why TTFT and ITL have to be separate SLIs. A single
"latency" metric would have shown a confusing average of two different effects.

**Queue wait is the whole story at 16-32 concurrency.** 1.7s and 4.7s TTFT
with 0% rejections means nothing failed — requests sat in the queue. This is
the attribution the dashboard's "Queue wait vs TTFT" panel is built for: when
the two lines track each other, add capacity; when TTFT rises alone, the
backend is slower and capacity will not help.

## Little's Law, applied

For a stable queue, `L = λW`: average queue length equals arrival rate times
average wait. Rearranged for the thing we actually care about:

```
queue wait ≈ queue_depth / service_rate
```

At concurrency 32 with 8 slots, 24 requests are typically waiting. Each
request occupies a slot for roughly `48 tokens / (55/1.42 tok/s) ≈ 1.24s`
(the 1.42 is the batch penalty at size 8). So:

```
expected wait ≈ 24 / (8 slots / 1.24 s) ≈ 3.7 s
```

Measured TTFT p95 at that step was 4.66s, which is 3.7s of predicted queue
wait plus ~0.14s prefill plus scheduling overhead and the p95 tail. The model
holds, which means it can be used to **size the queue** rather than guessing.

## Sizing `MAX_QUEUE_DEPTH`

The queue should be short enough that a request which is admitted can still
be served within the SLO. Anything longer is a lie told to the client.

```
max_queue ≈ TTFT_budget × (slots / time_per_request)
```

With a 500ms TTFT target, 8 slots, and 1.24s per request:

```
max_queue ≈ 0.5 × (8 / 1.24) ≈ 3
```

**The configured depth of 32 is roughly 10x too generous for the stated SLO.**
That is deliberate and left in place: it makes the queue-wait behaviour above
observable instead of being hidden behind instant rejections. A real
deployment honouring a 500ms TTFT SLO should run a queue of about 3-4 and shed
earlier, trading a higher 429 rate for a TTFT that is actually met.

This is the real tradeoff in inference serving, and it is not a tuning detail:
**you choose between turning users away quickly and making them wait.** There
is no setting that avoids both once you are past the knee.

## Caveat on the 429 numbers

The 99.3% rejection rate at concurrency 64 is inflated by the load generator.
Rejections return immediately, so each worker loops as fast as the network
allows and racks up rejected requests far faster than successful ones. The
*ratio* is therefore a property of the test harness, not of production
traffic; the meaningful signal is that shedding engaged at all, and at which
concurrency. A closed-loop generator with think time would report a far lower
rejection ratio for the same real user load.

## Where the simulator is and isn't trustworthy

The simulated backend reproduces three real behaviours: prefill cost scaling
with prompt length, per-request decode slowdown with batch size, and jittery
token timing. It does **not** model KV-cache eviction, paged-attention memory
pressure, speculative decoding, or GPU thermal throttling. The capacity
*shape* is right and the sizing maths transfers; the absolute tok/s numbers
are a property of this laptop and mean nothing for a real GPU.
