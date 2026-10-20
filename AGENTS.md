# AGENTS.md

Instructions for AI coding agents (Cursor, etc.) working in this repository.

## Ground rules

1. **Never claim something works without running a test that proves it.** Record what was run
   and the result in `TEST_EVIDENCE.md`; record feature state in `IMPLEMENTATION_STATUS.md`.
2. **No billable cloud resources without explicit human approval.** This includes
   `terraform apply`, `gcloud ... create`, and anything that creates GKE clusters, load
   balancers, static IPs, or Artifact Registry repos. `terraform plan` / `validate` are fine.
3. **No destructive commands without explicit human approval.** Examples: `kubectl delete`,
   `kind delete cluster`, `terraform destroy`, `git push --force`, `git reset --hard`,
   `rm -rf` outside build artifacts.
4. **Never hardcode or commit credentials.** No tokens, keys, kubeconfigs, service-account
   JSON, or `.tfstate` in the repo. Manifests reject credential-like env var names by design.
5. **Kubernetes context:** before any cluster command, display the current context and get
   approval. Only kind contexts (`kind-*`) may be used for automated tests.
6. **Namespace scope:** only `platformforge-dev` (the allowlist in
   `src/platformforge/manifest.py`). Never modify workloads in other namespaces. Expanding the
   allowlist is a reviewed code change, never a runtime option.
7. **Do not expose services publicly.** No NodePort, LoadBalancer, Ingress, or external IPs.
8. **No shell-outs built from manifest values.** Use the official Kubernetes Python client.
9. Implement incrementally, preserve existing behavior, and avoid unnecessary abstractions.
10. Do not begin a later milestone (GKE, observability, MCP) unless asked.
11. **Do not weaken the mesh.** Never switch `platformforge-dev` away from STRICT mTLS, widen
    the inventory AuthorizationPolicy, add mesh-wide policy in `istio-system`, or enable
    injection in another namespace without explicit approval. Negative tests must keep
    passing.

## Layout

```
src/platformforge/
  manifest.py     Pydantic schema, allowlist, YAML loading (no cluster access)
  resources.py    Pure builders: manifest -> V1Deployment / V1Service / V1Namespace / V1ServiceAccount
  kubecontext.py  Context discovery, display, approval, client construction
  deployer.py     Create/replace with ownership checks, rollout wait, status, logs
  cli.py          Typer CLI: validate, deploy, status, logs
services/
  pf_service_common/  Shared: JSON logging, request-ID middleware, health/version endpoints
  inventory_service/  FastAPI inventory-service
  orders_service/     FastAPI orders-service (calls inventory-service)
  manifests/          PlatformForge ServiceManifests (inventory v0.1.0 stable, v0.2.0 canary, orders)
  manifests/incidents/ Drill-only manifests (faulty canary); never the default deploy path
  Dockerfile          Multi-stage; --target inventory-service | orders-service
  requirements.txt    Exact runtime pins for the images
deploy/istio/     istioctl install config + istio-system namespace (PSA privileged for CNI)
deploy/istio/platformforge-dev/  Reviewed mesh policy: PeerAuthentication, AuthorizationPolicy,
                  DestinationRule, VirtualService (stable 100/0, canary 90/10)
docs/             ISTIO.md, CANARY_RELEASE.md, INCIDENT_RUNBOOK.md
examples/         Example ServiceManifest(s)
tests/unit/       Fast tests; Kubernetes API is mocked
tests/services/   Service unit tests (FastAPI TestClient, httpx MockTransport)
tests/integration Opt-in tests against a local kind cluster (mesh.py: in-cluster client
                  pods, Envoy admin/metrics/log helpers, allowlisted mesh-file apply)
.github/workflows CI: ruff, unit tests, image builds (no secrets, no cluster)
.tools/           gitignored; istioctl 1.31.2 at .tools/istio-1.31.2/bin/istioctl
```

## Commands

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e '.[dev]'
make lint                     # ruff check + format check on src, services, tests
make test                     # unit tests (integration skipped)
make images kind-load         # build arm64 0.2.0 images and load them into kind-platformforge
make images kind-load VERSION=0.1.0   # inventory 0.1.0 (stable) for the canary tests
make integration              # PLATFORMFORGE_IT_CONTEXT=kind-platformforge pytest -m integration -v
.tools/istio-1.31.2/bin/istioctl analyze -n platformforge-dev --context kind-platformforge  # read-only
```

Service dependency pins live in two places and must match: `services/requirements.txt`
(images) and the `dev` extra in `pyproject.toml` (tests).

## Conventions

- Every new manifest field must be explicit, bounded, and covered by a rejection test.
- Every API entry point in `deployer.py` must re-check the namespace allowlist and, for
  existing objects, the `app.kubernetes.io/managed-by=platformforge` label.
- Note: kubernetes client v37 models are Pydantic-based; attribute names are snake_case
  (e.g. `spec.external_ips`, not the older `external_i_ps`).
- Services: readiness must not depend on downstream services; upstream failures map to
  502/503/504 per request. Never log or forward an unvalidated inbound request ID.
- HTTP headers are not guaranteed to keep their case (Envoy normalizes them to lowercase; the
  API server proxy Go-canonicalizes them); compare header names case-insensitively in tests.

### Service mesh (Milestone 3)

- `istio-system` belongs to istioctl, not PlatformForge. Change it only through
  `deploy/istio/istio-operator.yaml` plus `istioctl install`, with approval. Never upgrade
  Istio without checking the supported-releases matrix for the cluster's Kubernetes version.
- PlatformForge must never render or accept Istio objects. Mesh policy lives in the reviewed
  files under `deploy/istio/platformforge-dev/`. Integration tests apply only the files
  allowlisted in `tests/integration/mesh.py` (`MESH_FILES`), only in `platformforge-dev`.
- `serviceAccountName` must equal `metadata.name`, because SPIFFE identity derives from it.
  Do not relax this rule.
- `version` changes the Deployment name. If a rename leaves an old Deployment, the CLI only
  warns. Deleting the old one is a manual step that needs approval: verify the new
  Deployment is serving first.
- Under STRICT, the API-server service proxy cannot reach the services. Test traffic goes
  through `pf-test-client-mesh` (sidecar, `sa/default`) or `pf-test-client-plain` (no
  sidecar), using `kubernetes.stream` exec of a fixed script with JSON arguments. Tests never
  delete these pods.
- Do not treat an HTTP 200 as proof of mTLS. Use the sidecar access log
  (`downstream_tls_version`, `*_peer_uri_san`) or `istio_requests_total`
  (`connection_security_policy`). Envoy TLS stats are trimmed and unavailable.
- After applying a VirtualService, wait for the callers' sidecars to report the new weights
  (`mesh.wait_for_weights`) before measuring traffic.
- Call services by name, never by pod IP. A sidecar sends pod-IP traffic through
  `PassthroughCluster` without mTLS, and STRICT rejects it.
