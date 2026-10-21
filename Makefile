VENV        ?= .venv
VERSION     ?= 0.2.0
GIT_SHA     ?= $(shell git describe --always --dirty 2>/dev/null || echo unknown)
PLATFORM    ?= linux/arm64
KIND_CLUSTER ?= platformforge
SERVICES    := inventory-service orders-service

GKE_CONTEXT ?= gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev
GKE_REGISTRY ?= us-central1-docker.pkg.dev/platformforge-dev-mn2026/platformforge

.PHONY: lint test images kind-load images-gke images-gke-publish gke-preflight integration mcp-live mcp-gke terraform-validate

lint:
	$(VENV)/bin/ruff check src services tests
	$(VENV)/bin/ruff format --check src services tests

test:
	$(VENV)/bin/pytest

images:
	@for svc in $(SERVICES); do \
		docker build --platform $(PLATFORM) --provenance=false --target $$svc \
			--build-arg VERSION=$(VERSION) --build-arg GIT_SHA=$(GIT_SHA) \
			-t platformforge/$$svc:$(VERSION) services || exit 1; \
	done

kind-load:
	@for svc in $(SERVICES); do \
		kind load docker-image platformforge/$$svc:$(VERSION) --name $(KIND_CLUSTER) || exit 1; \
	done

# Local linux/amd64 build for the GKE node pool. Does not push, create a
# repository, or enable an API. Tags are local until a separate publish approval.
images-gke:
	@for svc in $(SERVICES); do \
		docker build --platform linux/amd64 --provenance=false --target $$svc \
			--build-arg VERSION=$(VERSION) --build-arg GIT_SHA=$(GIT_SHA) \
			-t $(GKE_REGISTRY)/$$svc:$(VERSION) services || exit 1; \
	done
	@echo "Built $(GKE_REGISTRY)/*:$(VERSION) for linux/amd64. Not pushed."

images-gke-publish:
	@$(VENV)/bin/python -m platformforge.gke publish-requirements

# Read-only. Refuses kind contexts and any context other than GKE_CONTEXT.
gke-preflight:
	@$(VENV)/bin/python -m platformforge.gke preflight --context $(GKE_CONTEXT)

integration:
	PLATFORMFORGE_IT_CONTEXT=kind-$(KIND_CLUSTER) $(VENV)/bin/pytest -m integration -v

# Opt-in. Requires the kind reader ServiceAccount and loopback port-forwards.
# Does not run the deployment integration suite.
mcp-live:
	PLATFORMFORGE_MCP_LIVE=1 $(VENV)/bin/pytest tests/integration/test_mcp_live.py -v

# Opt-in. Requires the GKE reader ServiceAccount and loopback port-forwards.
# Does not contact kind or change workloads.
mcp-gke:
	PLATFORMFORGE_MCP_GKE=1 $(VENV)/bin/pytest tests/integration/test_mcp_gke.py -v

# Static checks only. Does not run terraform plan, apply, or destroy.
terraform-validate:
	terraform -chdir=infra/terraform fmt -check -recursive
	terraform -chdir=infra/terraform init -backend=false -input=false
	terraform -chdir=infra/terraform validate
