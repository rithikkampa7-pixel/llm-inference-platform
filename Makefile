.DEFAULT_GOAL := help
COMPOSE := docker compose
NET := $(shell docker network ls --format '{{.Name}}' | grep llm-inference-platform | head -1)
ARGS ?= --concurrency 16 --duration 60

.PHONY: help up down logs validate bench-image bench sweep saturate

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-13s\033[0m %s\n",$$1,$$2}'

up: ## Bring up gateway + Prometheus + Alertmanager + Grafana
	$(COMPOSE) up -d --build
	@echo
	@echo "  Grafana      http://localhost:3001  (LLM Inference — SLOs, Throughput and Cost)"
	@echo "  Prometheus   http://localhost:9091/alerts"
	@echo "  Gateway      http://localhost:8001/healthz"

down: ## Tear down, including stored metrics
	$(COMPOSE) down -v

logs: ## Follow delivered alerts
	$(COMPOSE) logs -f alert-sink

validate: ## Lint rules, config and dashboards (same checks CI runs)
	@echo "==> promtool: rules"
	@docker run --rm -v $(PWD)/observability/prometheus:/w --entrypoint promtool \
	  prom/prometheus:v3.1.0 check rules /w/rules/sli-recording.yaml /w/rules/slo-alerts.yaml
	@echo "==> promtool: config"
	@docker run --rm -v $(PWD)/observability/prometheus:/w --entrypoint promtool \
	  prom/prometheus:v3.1.0 check config /w/prometheus.yml
	@echo "==> amtool: alertmanager config"
	@docker run --rm -v $(PWD)/observability/alertmanager:/w --entrypoint amtool \
	  prom/alertmanager:v0.28.0 check-config /w/alertmanager.yml
	@echo "==> compose config"
	@$(COMPOSE) config -q
	@echo "==> dashboard JSON"
	@python3 -c "import json,glob;[json.load(open(f)) for f in glob.glob('observability/grafana/dashboards/*.json')];print('ok')"

bench-image: ## Build the load generator image
	@docker build -q -t llm-bench ./bench >/dev/null && echo "llm-bench built"

bench: bench-image ## Run the load generator. Override with ARGS="--concurrency 64 ..."
	@docker run --rm --network $(NET) llm-bench $(ARGS)

sweep: bench-image ## Capacity sweep — reproduces docs/capacity-model.md
	@for c in 1 4 8 16 32 64; do \
	  echo "============================================================"; \
	  docker run --rm --network $(NET) llm-bench \
	    --concurrency $$c --duration 20 --prompt-tokens 512 --max-tokens 48; \
	  sleep 5; \
	done

saturate: bench-image ## Push past the knee until TTFT breaches and shedding starts
	@docker run --rm --network $(NET) llm-bench --concurrency 64 --duration 150
