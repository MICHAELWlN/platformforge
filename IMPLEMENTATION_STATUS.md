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
  ServiceAccount/RBAC per service, no NetworkPolicy, no HPA/PDB. (Milestone 3 added an
  optional per-service ServiceAccount, with no RBAC bindings.)
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
| 2 | `/healthz`, `/readyz`, `/version` | Verified | unit tests; live (`test_services_kind::test_04`; since Milestone 3 via the in-cluster mesh test client, because STRICT mTLS blocks the API-server proxy) |
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
| 21 | Unit and integration tests | Verified | 77 service unit tests; 12 service integration tests (11 after the Milestone 3 migration merged the two `test_04` cases) |
| 22 | GitHub Actions CI (Ruff, unit tests, image builds) | **Written, not run on GitHub** | actionlint clean; every job step replayed locally (incl. amd64 build); repo has no GitHub remote yet |
| 23 | CI needs no GCP credentials or cluster | Verified by inspection | no `secrets.*`, no cloud auth, integration tests excluded |
| 24 | Existing 145 tests preserved | Verified | 234/234 passing (145 original + 89 new) |
| 25 | Docs updated | Written | README, ARCHITECTURE, IMPLEMENTATION_STATUS, TEST_EVIDENCE, AGENTS |

### Known limitations of Milestone 2

- ~~No authentication or authorization between services~~ **Addressed in Milestone 3**:
  STRICT mTLS and an identity-based AuthorizationPolicy on inventory-service. orders-service
  and demo-web still accept any mesh identity, and there is still no NetworkPolicy.
- ~~Plain HTTP inside the cluster~~ **Addressed in Milestone 3**: sidecar-to-sidecar
  traffic is TLSv1.3 mTLS. The applications themselves still speak plain HTTP to their own
  sidecar over localhost.
- No retries, circuit breaking, or rate limiting in orders-service. Istio's default retry
  policy applies at the sidecar; it did not retry HTTP 500 in the Milestone 3 drill.
- ~~No graceful connection draining~~ **Addressed in Milestone 3**: optional
  `termination.preStopSleepSeconds` / `gracePeriodSeconds`; both services use 5 s / 30 s.
  Draining under load has not been measured.
- Inventory data is a static in-memory dictionary.
- Python dependencies are pinned by version, not by hash (`--require-hashes` not used).
- No image signing, SBOM, or vulnerability scanning.
- Images are local-only (`platformforge/*:0.1.0`); not published to any registry.
  Re-building the same tag requires a new tag (or pod restart) to roll out, because the
  pull policy is `IfNotPresent`.
- CI builds `linux/amd64` only; arm64 is built locally.
- Starlette 1.9 emits a deprecation warning recommending `httpx2` for its TestClient
  (test-only; runtime uses httpx 0.28.1).

## Milestone 3: Istio service mesh (local kind only)

Istio 1.31.2 on the existing `kind-platformforge` cluster (Kubernetes v1.37.0). Details:
[docs/ISTIO.md](docs/ISTIO.md), [docs/CANARY_RELEASE.md](docs/CANARY_RELEASE.md),
[docs/INCIDENT_RUNBOOK.md](docs/INCIDENT_RUNBOOK.md); evidence in TEST_EVIDENCE.md M3.1–M3.10.

| Phase | Item | Status | Evidence |
| --- | --- | --- | --- |
| A | Compatible Istio installed (istioctl, minimal profile + CNI, native sidecars) | Verified | `test_a1`; precheck clean; istiod and CNI ready with 0 restarts |
| A | Injection only in `platformforge-dev`; PSA `restricted` kept | Verified | `test_a2`; PSA server dry-run with no warnings |
| A | Hardened sidecars in all three workloads (UID 1337, no capabilities, read-only FS, no `istio-init`) | Verified | `test_a3` ×3 |
| A | Existing apps still work after injection | Verified | M1 and M2 integration suites pass meshed (10 + 11) |
| B | Dedicated ServiceAccounts for orders and inventory, no token automount | Verified | `test_b1` ×2 |
| B | No unnecessary API permissions | Verified | `test_b2` ×2 (SubjectAccessReview denied; no bindings) |
| B | SPIFFE identities issued per ServiceAccount | Verified | `test_b3` ×2 (from Envoy certs) |
| B | Redeploy preserves identity | Verified | `test_b4` (pods unchanged, SA "updated") |
| B | Legacy → versioned inventory Deployment migration | Verified (one-time, manual, approved) | 8 gates passed; 1580/1580 probe requests returned 200 |
| C | STRICT mTLS, namespace-scoped (no mesh-wide policy) | Verified | `test_c1`; `istioctl x describe` |
| C | Plaintext from a non-mesh client is rejected (negative test) | Verified | `test_c2` ×2, with a PERMISSIVE control that returned 200 |
| C | Encryption proven by both sidecars, not by HTTP status | Verified | `test_c3`, `test_c5` (TLSv1.3 with SPIFFE SANs on both ends), `test_c6` (`mutual_tls` metric) |
| C | Bypassing service discovery does not bypass STRICT | Verified | `test_c4` (pod IP → PassthroughCluster → 503 UC) |
| D | inventory accepts only orders-service, GET `/inventory/*` | Verified | `test_d1`, `test_d2` |
| D | Other identities and operations denied (negative tests) | Verified | `test_d3` (default SA → 403), `test_d4` ×4 |
| D | Policy is workload-scoped; health checks unaffected | Verified | `test_d5`, `test_d6` |
| E | inventory v0.2.0 alongside v0.1.0, subsets and weighted VirtualService | Verified | `test_e1`, `test_e2` (stable: 0/200 to v0.2.0) |
| E | ~90/10 split measured | Verified | `test_e3`: 902/98, 887/113, 919/81, 912/88 across four runs (9.5% pooled) |
| F | Injected canary failure is observable | Verified | `test_f1`: 42, 49, 46, 44 of 500 returned 502, each matched to a v0.2.0 500 in mesh metrics |
| F | Traffic shifted to healthy version; recovery verified | Verified | `test_f2`: converged in 1.1 s; 300/300 returned 200 |
| F | Stable configuration restored | Verified | `test_f3` |
| G | Manifest fields `version`, `serviceAccountName`, `termination` (strict, bounded) | Verified | `tests/unit/test_mesh_fields.py` (65) |
| H | Full regression | Verified | 344/344 (289 unit + 55 integration) |
| I | Documentation | Written | README, ARCHITECTURE, AGENTS, this file, TEST_EVIDENCE, docs/* |

### Known limitations of Milestone 3

- Single-node kind only. Not tested on GKE, multi-node, or under load.
- Istio's self-signed root CA (in the `istio-system` Secret) and 24h workload certificates.
  No external CA; rotation has not been exercised.
- Only inventory-service has an AuthorizationPolicy. There is no namespace default-deny,
  and orders-service and demo-web accept any mesh identity.
- Egress is `ALLOW_ANY` and there is no NetworkPolicy. A non-mesh pod can still open TCP
  connections; it just cannot get a response.
- Envoy TLS handshake stats are trimmed by Istio. Encryption evidence is from access logs and
  Istio metrics, not packet capture.
- Istio objects (PeerAuthentication, AuthorizationPolicy, DestinationRule, VirtualService) are
  applied with `kubectl`, not by PlatformForge. The CLI deliberately does not manage
  arbitrary Istio objects.
- Canary weights are changed by applying one of two reviewed VirtualService files. There is
  no automated progressive delivery or metric-based rollback.
- The `kubectl` API-server proxy to meshed services no longer works under STRICT. Tests use
  in-cluster client pods instead (`pf-test-client-mesh`, `pf-test-client-plain`). The tests
  create and reuse these pods and never delete them.
- No observability stack yet. Evidence comes from Envoy admin endpoints and access logs.
- Removing an old Deployment after a rename (`inventory-service` → `inventory-service-v0-1-0`)
  is a manual, approval-gated step. The CLI only warns about the overlap.

## Later milestones

| Area | Status |
| --- | --- |
| Terraform (GCP network, GKE, Artifact Registry) | Not started |
| GKE workload deployment | Not started |
| Istio (STRICT mTLS, traffic management, canary) | **Verified on local kind** (Milestone 3); not on GKE |
| OpenTelemetry, Prometheus, Loki, Tempo, Grafana | Not started |
| CD (publish images, deploy from CI) | Not started |
| Read-only MCP server | Not started |
| Incident simulation and rollback | Partial: canary failure drill and remediation (Milestone 3); no automated rollback |
