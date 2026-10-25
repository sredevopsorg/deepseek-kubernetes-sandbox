# Canonical local entrypoint. Includes versions.env so there is exactly one
# place to bump a version (run "make check" to verify pins across the repo).
#
#   make image        build the main DSH Web image
#   make gateway      build the example gateway image
#   make test         gateway unit tests
#   make lint         ruff + shellcheck (+ yamllint/actionlint when installed)
#   make check        versions, example layout and manifest YAML checks
#   make e2e          full example walkthrough on a kind cluster
#   make changelog    regenerate CHANGELOG.md with git-cliff
include versions.env

RELEASE_IDENTITY := $(DSH_VERSION)$(if $(RELEASE_REVISION),-$(RELEASE_REVISION))
IMAGE ?= $(GHCR_IMAGE):$(RELEASE_IDENTITY)
GATEWAY_LOCAL_IMAGE ?= dsh-aaas-gateway:dev
GATEWAY_DIR := examples/dsh-agents-as-a-service/gateway
EXAMPLE_DIR := examples/dsh-agents-as-a-service
PYTHON ?= $(if $(wildcard $(CURDIR)/.venv/bin/python),$(CURDIR)/.venv/bin/python,python3)
DSH_PORT ?= 3080
VCS_REF ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)

.PHONY: help image run gateway test cov lint check e2e changelog release-dry-run set-version clean

help: ## Show available targets
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /	/'

image: ## Build the main DSH Web image (no push)
	docker build \
		--build-arg DSH_VERSION=$(DSH_VERSION) \
		--build-arg RELEASE_REVISION=$(RELEASE_REVISION) \
		--build-arg PNPM_VERSION=$(PNPM_VERSION) \
		--build-arg VCS_REF=$(VCS_REF) \
		-t $(IMAGE) .

run: image ## Build and run the main image (needs DEEPSEEK_API_KEY)
	docker run --rm -it \
		-p $(DSH_PORT):3080 \
		-v dsh-home:/home/node/.dsh \
		-e DEEPSEEK_API_KEY \
		$(IMAGE)

gateway: ## Build the example gateway image locally
	docker build -t $(GATEWAY_LOCAL_IMAGE) $(GATEWAY_DIR)/

test: ## Gateway unit tests
	cd $(GATEWAY_DIR) && $(PYTHON) -m pytest -q

cov: ## Gateway unit tests with coverage (needs pytest-cov)
	cd $(GATEWAY_DIR) && $(PYTHON) -m pytest -q --cov=. --cov-report=term-missing --cov-report=xml:coverage.xml

lint: ## Lint Python, shell, YAML and Dockerfiles (skips missing tools locally)
	cd $(GATEWAY_DIR) && $(PYTHON) -m ruff check .
	cd $(GATEWAY_DIR) && $(PYTHON) -m ruff format --check .
	shellcheck build.sh scripts/*.sh $(EXAMPLE_DIR)/scripts/*.sh
	@command -v yamllint >/dev/null 2>&1 && yamllint . || echo "yamllint not installed; CI enforces it"
	@command -v actionlint >/dev/null 2>&1 && actionlint || echo "actionlint not installed; CI enforces it"

check: ## Verify version pins, example layout and manifest YAML
	./scripts/check-versions.sh
	./scripts/check-examples.sh
	$(PYTHON) scripts/check-manifests.py

e2e: ## Run the full example walkthrough on the current kind cluster
	$(EXAMPLE_DIR)/scripts/e2e-kind.sh

changelog: ## Regenerate CHANGELOG.md from Conventional Commits
	@command -v git-cliff >/dev/null 2>&1 || { echo "install git-cliff: https://git-cliff.org" >&2; exit 1; }
	git-cliff --output CHANGELOG.md

release-dry-run: check ## Validate the release identity and preview release notes
	@echo "release identity: $(RELEASE_IDENTITY)"
	@echo "main image:       $(GHCR_IMAGE):$(RELEASE_IDENTITY)"
	@echo "gateway image:    $(GATEWAY_IMAGE):$(RELEASE_IDENTITY)"
	@if git rev-parse -q --verify "refs/tags/$(RELEASE_IDENTITY)" >/dev/null; then echo "ERROR: tag already exists" >&2; exit 1; fi
	@command -v git-cliff >/dev/null 2>&1 && git-cliff --unreleased --tag $(RELEASE_IDENTITY) || echo "git-cliff not installed; skipping notes preview"

set-version: ## Bump pins: make set-version DSH_VERSION=dsh-vX [RELEASE_REVISION=rN]
	@test -n "$(DSH_VERSION)" || { echo "DSH_VERSION is required" >&2; exit 1; }
	./scripts/set-version.sh "$(DSH_VERSION)" "$(RELEASE_REVISION)"

clean: ## Remove local build/test caches
	rm -rf .pytest_cache .ruff_cache $(GATEWAY_DIR)/.pytest_cache $(GATEWAY_DIR)/.ruff_cache $(GATEWAY_DIR)/__pycache__
