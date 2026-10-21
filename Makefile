VENV        ?= .venv
VERSION     ?= 0.2.0
GIT_SHA     ?= $(shell git describe --always --dirty 2>/dev/null || echo unknown)
PLATFORM    ?= linux/arm64
KIND_CLUSTER ?= platformforge
SERVICES    := inventory-service orders-service

GKE_CONTEXT ?= gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev
GKE_REGISTRY ?= us-central1-docker.pkg.dev/platformforge-dev-mn2026/platformforge

.PHONY: lint test images kind-load images-gke images-gke-publish gke-preflight integration mcp-live mcp-gke canary-kind canary-controller-kind terraform-validate demo-preflight demo-forwards demo-reader demo-controller demo-evidence demo-stop demo-connect demo-connect-status demo-connect-stop

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

# Opt-in kind drill. Deploys the faulty canary, then restores the healthy one.
# Refuses every context other than kind-platformforge.
canary-kind:
	PLATFORMFORGE_CANARY_IT=1 $(VENV)/bin/pytest tests/integration/test_canary_kind.py -v -s

# Opt-in. The controller Deployment must already be ready on kind-platformforge.
# Does not build the image and does not run the one-shot drill.
canary-controller-kind:
	PLATFORMFORGE_CANARY_IT=1 $(VENV)/bin/pytest tests/integration/test_canary_controller_kind.py -v -s

# Read-only demo helpers. Failure injection and route restore stay behind
# CONFIRM_FAILURE and CONFIRM_RESTORE in scripts/demo and are not Make targets.
demo-preflight:
	./scripts/demo/preflight.sh

demo-forwards:
	./scripts/demo/port-forwards.sh

demo-reader:
	./scripts/demo/reader-token.sh

demo-controller:
	./scripts/demo/controller-ready.sh

demo-evidence:
	./scripts/demo/collect-evidence.sh

demo-stop:
	./scripts/demo/stop-forwards.sh

# Local supervisor for the recording port-forwards. Does not change the cluster.
demo-connect:
	./scripts/demo/connect.sh start

demo-connect-status:
	./scripts/demo/connect.sh status

demo-connect-stop:
	./scripts/demo/connect.sh stop

# Static checks only. Does not run terraform plan, apply, or destroy.
terraform-validate:
	terraform -chdir=infra/terraform fmt -check -recursive
	terraform -chdir=infra/terraform init -backend=false -input=false
	terraform -chdir=infra/terraform validate
