VENV        ?= .venv
VERSION     ?= 0.1.0
GIT_SHA     ?= $(shell git describe --always --dirty 2>/dev/null || echo unknown)
PLATFORM    ?= linux/arm64
KIND_CLUSTER ?= platformforge
SERVICES    := inventory-service orders-service

.PHONY: lint test images kind-load integration

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

integration:
	PLATFORMFORGE_IT_CONTEXT=kind-$(KIND_CLUSTER) $(VENV)/bin/pytest -m integration -v
