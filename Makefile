# Local build and test helpers. Every variable can be overridden, e.g.
#   make image DSH_VERSION=dsh-v0.2.1-rc.2 IMAGE=dsh-web:dev
#   make test
DSH_VERSION ?= dsh-v0.2.1-rc.2
PNPM_VERSION ?= 11.28.5
IMAGE ?= dsh-web:0.2.1-rc.2
GATEWAY_IMAGE ?= ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:gateway
GATEWAY_DIR := examples/dsh-agents-as-a-service/gateway
PYTHON ?= python3
DSH_PORT ?= 3080

.PHONY: help image gateway run test lint

help: ## Show available targets
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

image: ## Build the DSH Web image
	docker build \
		--build-arg DSH_VERSION=$(DSH_VERSION) \
		--build-arg PNPM_VERSION=$(PNPM_VERSION) \
		-t $(IMAGE) .

gateway: ## Build the gateway image
	docker build -t $(GATEWAY_IMAGE) $(GATEWAY_DIR)/

run: image ## Build and run the DSH Web image
	docker run --rm -it \
		-p $(DSH_PORT):3080 \
		-v dsh-home:/home/node/.dsh \
		-e DEEPSEEK_API_KEY \
		$(IMAGE)

test: ## Run the gateway unit tests (needs requirements + requirements-dev installed)
	cd $(GATEWAY_DIR) && $(PYTHON) -m pytest -q

lint: ## Lint the gateway with ruff
	cd $(GATEWAY_DIR) && ruff check .
