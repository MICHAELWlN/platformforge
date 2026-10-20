# Implementation Status

Last updated: 2026-10-20. Status reflects only what has been executed and verified; see
[TEST_EVIDENCE.md](TEST_EVIDENCE.md).

Legend: **Verified** = implemented and exercised by passing tests (unit and/or live kind
cluster). **Not started** = no code exists.

## Milestone 1: CLI and Kubernetes deployment engine

| # | Item | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Python project structure, `pyproject.toml` | Verified | `pip install -e '.[dev]'` succeeds; `platformforge` entry point works |
| 2 | Typer CLI | Verified | `tests/unit/test_cli.py`, manual runs |
| 3 | Pydantic manifest validation | Verified | `tests/unit/test_manifest.py` (80 cases) |
| 4 | Example service definition | Verified | `examples/demo-web.yaml` validates and deploys on kind |
| 5 | Kubernetes deployment module | Verified | unit (mocked API) + live kind |
| 6a | `platformforge validate` | Verified | unit + manual |
| 6b | `platformforge deploy` | Verified | live kind: create path, update path, rollout wait |
| 6c | `platformforge status` | Verified | live kind: reports 2/2 available, HEALTHY |
| 6d | `platformforge logs` | Verified | live kind: logs from both pods |
| 7 | Namespace-scoped deployment | Verified | allowlist unit tests; live check that managed objects exist only in `platformforge-dev` |
| 8 | Readiness and liveness probes | Verified | live Deployment spec checked; pods Ready; kube-probe requests in logs |
| 9 | Resource requests and limits | Verified | live Deployment spec checked |
| 10 | Unit and integration tests | Verified | 135 unit + 10 integration passing (still passing after Milestone 2) |
| 11–15 | README, AGENTS, ARCHITECTURE, IMPLEMENTATION_STATUS, TEST_EVIDENCE | Written | n/a |

### Security requirements

| Requirement | Status | How |
| --- | --- | --- |
| Validate service and namespace names | Verified | DNS-1035 / DNS-1123 regex + length |
| Explicit namespace allowlist | Verified | `ALLOWED_NAMESPACES` in code; re-checked in `deployer` |
| Reject arbitrary Kubernetes objects | Verified | fixed `apiVersion`/`kind`, `extra="forbid"` everywhere |
| No shell commands from manifest values | Verified by construction | no `subprocess`/`os.system` in `src/`; client library only |
| Display context and require approval | Verified | interactive prompt (default No) or exact-match `--approve-context` |
| No modification outside dev namespace | Verified | allowlist + ownership label check before writes |
| No public exposure | Verified | Service type hardcoded `ClusterIP`; live check |

### Known limitations of Milestone 1

- Only one namespace (`platformforge-dev`) and one container per service.
- No `delete`/`rollback` command (deliberately out of scope; deletion is manual).
- Uses create/replace, not server-side apply; fields added to these objects by other
  controllers or by `kubectl edit` are overwritten on the next deploy.
- No private-registry `imagePullSecrets`, no ConfigMap/Secret references, no
  ServiceAccount/RBAC per service, no NetworkPolicy, no HPA/PDB.
- Credential detection for env vars is a name-based heuristic; it cannot detect secrets in
  values.
- The CLI runs with the caller's own kube credentials. The allowlist protects against
  mistakes, not against a user who can already run `kubectl` against other namespaces.
- `logs` does not support `--follow` or previous-container logs.
- Integration tests have only been run on kind v0.33 / Kubernetes v1.37 (single node).
- Validated on macOS arm64 with Python 3.13 only. Python 3.11/3.12/3.14 not tested.

## Milestone 2: microservice foundation

| # | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Both services in FastAPI | Verified | `services/inventory_service`, `services/orders_service` |
| 2 | `/healthz`, `/readyz`, `/version` | Verified | unit tests; live via API-server proxy (`test_services_kind::test_04`) |
| 3 | `GET /inventory/{item_id}` | Verified | unit + live |
| 4 | `GET /orders/check/{item_id}` | Verified | unit + live |
| 5 | orders → inventory via Service DNS | Verified | live: orders log shows upstream `inventory-service.platformforge-dev.svc.cluster.local`; DNS resolves to the ClusterIP from inside an orders pod |
| 6 | Configurable upstream URL and timeout | Verified | `Settings` unit tests; values set via manifest env |
| 7 | Errors when inventory is unavailable | Verified | unit (503/504/502 paths); live outage returned 503 (and 504 transiently) |
| 8 | Structured JSON logging | Verified | unit; integration parses every pod log line as JSON |
| 9 | Request IDs propagated | Verified | unit (in-process e2e); live: same ID in orders and inventory pod logs |
| 10 | Minimal images | Verified | distroless runtime; 28.5 MB stored on the kind node |
| 11 | Non-root | Verified | image `User=65532:65532`; live `/proc/1/status` Uid 65532 |
| 12 | Read-only root FS | Verified | live write to `/app` fails with `EROFS`; services run normally |
| 13 | No unnecessary capabilities | Verified | live `CapEff: 0000000000000000`, `NoNewPrivs: 1` |
| 14 | Manifests compatible with existing CLI | Verified | `platformforge validate` OK; no CLI/schema changes |
| 15 | Readiness and liveness probes | Verified | `/readyz` and `/healthz` in manifests; pods Ready |
| 16 | Requests and limits | Verified | 50m/64Mi requests, 250m/128Mi limits; live spec checked |
| 17 | Local arm64 builds | Verified | `docker image inspect`: `linux/arm64` |
| 18 | Images loaded into kind | Verified | `kind load docker-image`; `crictl images` on the node |
| 19 | Deployed with PlatformForge | Verified | `platformforge deploy` for both (integration + manual) |
| 20 | Service-to-service communication | Verified | live 200 responses with inventory data |
| 21 | Unit and integration tests | Verified | 77 service unit tests; 12 service integration tests |
| 22 | GitHub Actions CI (Ruff, unit tests, image builds) | **Written, not run on GitHub** | actionlint clean; every job step replayed locally (incl. amd64 build); repo has no GitHub remote yet |
| 23 | CI needs no GCP credentials or cluster | Verified by inspection | no `secrets.*`, no cloud auth, integration tests excluded |
| 24 | Existing 145 tests preserved | Verified | 234/234 passing (145 original + 89 new) |
| 25 | Docs updated | Written | README, ARCHITECTURE, IMPLEMENTATION_STATUS, TEST_EVIDENCE, AGENTS |

### Known limitations of Milestone 2

- No authentication or authorization between services, and no NetworkPolicy: any pod in the
  cluster can call either service. (Planned: Istio STRICT mTLS + AuthorizationPolicy.)
- Plain HTTP inside the cluster (no TLS) until the service mesh milestone.
- No retries, circuit breaking, or rate limiting in orders-service.
- No graceful connection draining (`preStop` hook) — the PlatformForge manifest has no
  lifecycle field. During scale-down, in-flight calls to terminating inventory pods produced
  a 504 (timeout) before endpoints were removed and calls switched to fast 503s.
- Inventory data is a static in-memory dictionary.
- Python dependencies are pinned by version, not by hash (`--require-hashes` not used).
- No image signing, SBOM, or vulnerability scanning.
- Images are local-only (`platformforge/*:0.1.0`); not published to any registry.
  Re-building the same tag requires a new tag (or pod restart) to roll out, because the
  pull policy is `IfNotPresent`.
- CI builds `linux/amd64` only; arm64 is built locally.
- Starlette 1.9 emits a deprecation warning recommending `httpx2` for its TestClient
  (test-only; runtime uses httpx 0.28.1).

## Later milestones

| Area | Status |
| --- | --- |
| Terraform (GCP network, GKE, Artifact Registry) | Not started |
| GKE workload deployment | Not started |
| Istio (STRICT mTLS, traffic management, canary) | Not started |
| OpenTelemetry, Prometheus, Loki, Tempo, Grafana | Not started |
| CD (publish images, deploy from CI) | Not started |
| Read-only MCP server | Not started |
| Incident simulation and rollback | Not started (manual outage simulation exists as a test only) |
