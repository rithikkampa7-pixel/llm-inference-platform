# Runbook — TTFT and inference alerts

## TTFTErrorBudgetBurnFast

**Means:** more than 72% of streaming requests (14.4 × the 5% budget) are
taking over 500ms for a first token.

### 1. Saturation or backend? (30 seconds)

This single question determines the fix, and the dashboard panel
"Queue wait vs TTFT" answers it:

```bash
P=http://localhost:9091/api/v1/query
curl -s --get $P --data-urlencode 'query=llm:slot_utilisation:ratio'
curl -s --get $P --data-urlencode 'query=histogram_quantile(0.95, sum by (le) (rate(llm_queue_wait_seconds_bucket[5m])))'
```

- **Utilisation near 1.0 and queue wait rising** → saturation. Scaling helps.
  Go to step 2.
- **Utilisation well below 1.0 and queue wait flat** → the backend got slower
  on its own. Scaling will not help. Go to step 3.

### 2. Saturation path

Check whether it is more traffic or more work per request:

```bash
curl -s --get $P --data-urlencode 'query=sum(rate(llm_requests_total[5m]))'
curl -s --get $P --data-urlencode 'query=sum(rate(llm_tokens_total{direction="prompt"}[5m])) / sum(rate(llm_requests_total{outcome="ok"}[5m]))'
```

The second query is average prompt size. A jump there means a client started
sending much longer prompts — prefill is linear in prompt length, so TTFT
degrades without any traffic increase at all. That is a client-side
regression, and the fix is a prompt-size limit, not more GPUs.

Otherwise, add capacity:

```bash
DECODE_SLOTS=16 docker compose up -d gateway
```

Note from [capacity-model.md](./capacity-model.md): raising slots raises
throughput *and* inter-token latency, because batch size rises. Do not raise
slots past the point where ITL breaks the user experience — check the ITL
heatmap after scaling, not just TTFT.

### 3. Backend path

```bash
docker compose logs --tail=100 gateway
curl -s http://localhost:8001/healthz
```

With `BACKEND=openai`, the model server is a separate failure domain — check
its own metrics and logs. The gateway cannot fix a slow model; what it can do
is stop queueing work that will miss the SLO anyway, which means lowering
`MAX_QUEUE_DEPTH` to shed earlier.

### 4. If you cannot fix it quickly

Shed load deliberately rather than letting everyone experience a 7-second
wait. Lowering the queue converts a latency incident into a (more honest)
availability-of-capacity incident:

```bash
MAX_QUEUE_DEPTH=4 docker compose up -d gateway
```

This will make `LoadSheddingActive` fire. That is the correct trade when TTFT
cannot be met: fast rejection with `Retry-After` beats an unbounded wait,
because the client can make a decision.

## LoadSheddingActive

The queue is full and users are getting 429s. The system is protecting itself
correctly — this is a **capacity shortfall, not a bug**.

```bash
curl -s --get $P --data-urlencode 'query=sli:shed:reject_ratio_rate5m'
curl -s --get $P --data-urlencode 'query=sum(llm_queue_depth)'
```

Scale decode slots or replicas. Check `DecodeSlotsSaturated` history — it
should have ticketed ~10 minutes before this paged. If it did not, the
saturation threshold needs lowering.

## InferenceAvailabilityBurnFast

Genuine errors, not overload — rejections are excluded from this SLI by
construction. Treat it as a normal backend failure: check logs, check recent
deploys, roll back if correlated.

If this and `LoadSheddingActive` fire together, you have a backend failing
*under* overload. Fix the errors first; shedding is at least intentional.

## InferenceSpendAnomaly

```bash
curl -s --get $P --data-urlencode 'query=llm:cost_usd_per_hour:rate5m'
curl -s --get $P --data-urlencode 'query=sum by (direction) (rate(llm_tokens_total[5m]))'
```

- **Prompt tokens up** → a client is sending bigger prompts. Also degrades TTFT.
- **Completion tokens up** → longer generations, or a retry loop duplicating
  work. Check `llm_requests_total{outcome="cancelled"}`: many cancellations
  with high spend means clients are timing out and retrying, and you are
  paying for abandoned work twice.

## Reproducing each alert

```bash
make up

# TTFT breach + load shedding (concurrency far past the knee)
make bench ARGS="--concurrency 64 --duration 120"

# Saturation warning only, no breach
make bench ARGS="--concurrency 10 --duration 600"

# Spend anomaly: same traffic, 8x the prompt size
make bench ARGS="--concurrency 8 --duration 120 --prompt-tokens 8192"

docker compose logs -f alert-sink
```
