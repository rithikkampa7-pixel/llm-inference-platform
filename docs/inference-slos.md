# SLOs for an LLM endpoint

## Why the usual SLIs don't work here

A normal web service gets one latency SLI because request duration is roughly
constant for a given endpoint. An LLM endpoint breaks that assumption
completely:

- **Duration scales with output length.** A request generating 2,000 tokens
  takes ~35x longer than one generating 60. Putting both in one histogram
  measures the distribution of `max_tokens`, not the health of the service.
- **Users perceive the first token, not the last.** A 20-second response that
  starts streaming in 200ms feels responsive. A 3-second response that stalls
  silently for 2.8s feels broken. Total duration ranks these backwards.
- **Latency and throughput are in direct tension.** Batching more requests
  raises tokens/sec and lowers per-request speed. One metric cannot express a
  tradeoff; you need both sides of it.

## The three SLOs

| SLO | SLI | Target | Budget |
|---|---|---|---|
| **TTFT** | share of streaming requests whose first token took >500ms | 95% within 500ms | 5% |
| **Availability** | errors / (ok + errors) | 99.9% succeed | 0.1% |
| **Shed** | rejected / all requests | <1% rejected | 1% |

### Why TTFT has a 5% budget and availability has 0.1%

They are different kinds of promise. A failed request is a broken promise; a
slow first token is a degraded one. Users retry slowness and abandon errors.
Holding TTFT to 99.9% would mean provisioning for peak concurrency at all
times, which for GPU capacity is ruinously expensive and buys little — so the
budget is deliberately loose and the alerting thresholds scale with it
(`14.4 × 0.05`, not `14.4 × 0.001`).

### Why 429s get their own SLO

A rejected request is not a server error, and it is not a success either.
Folding it into availability would be wrong in both directions:

- Counting 429 as a failure means one capacity event drains the availability
  budget, and the page tells you "requests are failing" when the real message
  is "buy more GPUs". Different team, different fix.
- Ignoring 429 entirely means the gateway can shed 100% of traffic and report
  perfect availability. The SLI would be measuring the queue, not the service.

So availability is computed over **accepted** requests only
(`outcome=~"ok|error"`), and rejection gets a separate 1% budget. Two budgets,
two causes, two remedies. When both burn at once you have a backend failure
*under* overload, and the dashboard shows it as two distinct lines.

Cancelled requests (the client hung up mid-stream) are counted in a third
bucket and excluded from both. They are common, usually not the server's
fault, and counting them as successes would overstate health.

### Why TTFT excludes non-streaming requests

TTFT is not user-visible on a blocking call — the client sees one response at
the end. The SLI filters on `stream="true"` so it measures one coherent
experience. Non-streaming TTFT is still *recorded* (as `stream="false"`),
because it is a useful backend-health signal, but it is not in the SLO.

## Queue wait is measured separately, and TTFT includes it

`llm_queue_wait_seconds` is its own histogram, but TTFT is timed from request
arrival and therefore *contains* queue wait.

That is deliberate. Measuring TTFT from the moment work starts would let the
gateway hide its own saturation: an overloaded system would report excellent
TTFT for the few requests it got around to serving. The user waited, so the
SLI counts the wait.

Having both lets you attribute an incident in one glance:

| Queue wait | TTFT | Meaning | Fix |
|---|---|---|---|
| rising | rising | saturation | scale out |
| flat | rising | backend slower | investigate the model server |
| rising | flat | — | impossible; check your instrumentation |

## Leading indicator vs SLO breach

`DecodeSlotsSaturated` fires at >90% slot utilisation for 10 minutes and is a
**ticket, not a page**. It is not a breach of anything — it is the warning
that the next traffic increase becomes queue wait, and queue wait becomes
TTFT. The capacity curve in [capacity-model.md](./capacity-model.md) shows the
cliff is steep: between concurrency 8 and 16, TTFT p95 goes from 144ms to
1,679ms with no throughput gain. Ten minutes of warning is worth more than a
faster page after the fact.

## Cost as an operational signal

`InferenceSpendAnomaly` compares the current hourly burn to its own 6-hour
baseline and tickets on a 3x jump. Inference is one of the few workloads where
a bug is immediately, measurably expensive: a retry storm or a prompt-size
regression shows up in `llm_cost_usd_total` within minutes, long before anyone
reads a monthly bill. Splitting `llm_tokens_total` by direction tells you
which — prompt tokens growing means prompts got bigger, completion tokens
growing means generations got longer or retries are duplicating work.

## Known limitations

- **Histogram buckets saturate under extreme overload.** Queue wait tops out
  at a 5s finite bucket and TTFT at 10s, so `histogram_quantile` clamps there.
  During the concurrency-64 sweep step, real TTFT exceeded the top bucket and
  the reported p95 is a floor, not a measurement. Acceptable: anything past
  10s is equally catastrophic. If you need resolution there, add buckets.
- **No minimum-traffic guard on the ratio SLIs.** At low QPS a single error is
  a large ratio. See the equivalent discussion in the companion
  `sre-slo-platform` repo; the fix is an `and sum(rate(...)) > N` clause.
- **Single replica.** The SLIs aggregate correctly across replicas by
  construction (counter ratios, not quantiles), but no multi-replica routing
  or per-replica fairness is implemented here.
