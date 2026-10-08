#!/usr/bin/env bash
# Capacity sweep: step concurrency up and record where TTFT breaks.
#
# This is how the numbers in docs/capacity-model.md were produced. The point
# is to find the knee -- the concurrency at which throughput stops improving
# but latency keeps getting worse.
set -euo pipefail

DURATION="${DURATION:-45}"
PROMPT="${PROMPT:-512}"
MAXTOK="${MAXTOK:-64}"

for c in 1 2 4 8 16 32 64; do
  echo "============================================================"
  python3 bench/loadgen.py \
    --concurrency "$c" --duration "$DURATION" \
    --prompt-tokens "$PROMPT" --max-tokens "$MAXTOK"
  sleep 5
done
