"""Prometheus metrics for LLM inference.

Serving an LLM is not like serving a REST endpoint, and the usual
request-duration histogram hides almost everything that matters:

  * A request's total duration is dominated by how many tokens it generated,
    so comparing durations across requests compares prompt lengths, not
    service health.
  * Users perceive *time to first token*, not total time. A 20s response that
    starts streaming in 300ms feels fast; a 3s response that stalls for 2.8s
    feels broken.
  * Throughput per request degrades as concurrency rises, because decode
    steps are batched. Latency and utilisation are in direct tension, which a
    single latency metric cannot express.

So the SLIs here are TTFT, inter-token latency, and queue wait -- measured
separately -- plus token counters for cost.
"""

from prometheus_client import Counter, Gauge, Histogram

# Time to first token. Buckets cluster tightly below 1s because that is where
# the perceptual difference lives; beyond ~5s the user has already left.
TTFT = Histogram(
    "llm_time_to_first_token_seconds",
    "Time from request admission to the first streamed token.",
    ["model", "stream"],
    buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0, 10.0),
)

# Inter-token latency: the gap between successive tokens. This is what makes
# a stream feel smooth or stuttery, and it degrades under batching pressure.
ITL = Histogram(
    "llm_inter_token_latency_seconds",
    "Gap between consecutive streamed tokens.",
    ["model"],
    buckets=(0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.2, 0.5),
)

# Queue wait is separated from service time on purpose. Rising queue wait with
# flat service time means a capacity problem; the reverse means the model or
# backend got slower. Collapsing them into one number loses that distinction.
QUEUE_WAIT = Histogram(
    "llm_queue_wait_seconds",
    "Time spent waiting for a decode slot before work began.",
    ["model"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

REQUESTS = Counter(
    "llm_requests_total",
    "Inference requests by outcome.",
    ["model", "outcome"],  # ok | error | rejected | cancelled
)

TOKENS = Counter(
    "llm_tokens_total",
    "Tokens processed, split by direction.",
    ["model", "direction"],  # prompt | completion
)

# Cost is a first-class operational metric for inference, not an afterthought
# for finance. A latency regression that doubles retries doubles spend.
COST = Counter(
    "llm_cost_usd_total",
    "Cumulative inference spend in USD.",
    ["model"],
)

QUEUE_DEPTH = Gauge(
    "llm_queue_depth",
    "Requests admitted but not yet started.",
    ["model"],
)

ACTIVE = Gauge(
    "llm_active_requests",
    "Requests currently decoding (the effective batch size).",
    ["model"],
)

CAPACITY = Gauge(
    "llm_decode_slots",
    "Configured concurrent decode slots.",
    ["model"],
)
