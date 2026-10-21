# Implementation Status

Last updated: 2026-10-21. Status reflects only what has been executed and verified; see
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
- Milestone 3 evidence was collected from Envoy admin endpoints and access logs.
  Milestone 4 adds Prometheus, Loki, and Tempo; those checks are below.
- Removing an old Deployment after a rename (`inventory-service` → `inventory-service-v0-1-0`)
  is a manual, approval-gated step. The CLI only warns about the overlap.

## Milestone 4: observability (local kind only)

| Phase | Scope | Status | Evidence |
| --- | --- | --- | --- |
| A | Capacity | Fits in the current Docker VM | requests 1860m / 2562Mi after install; node at 3.792 GiB / 7.748 GiB |
| B | Prometheus, Grafana, Loki, Tempo, Alloy | Healthy, ClusterIP | helm list; all pods Ready |
| C | Istio metrics for both services | Verified | scrape targets up; `istio_requests_total` increases |
| D | OTel traces, both services, parent span | Verified | Tempo trace `20f98e2b62278403629b59cfd4da5f07` |
| E | Loki logs with request and trace IDs | Verified | orders `2026-10-20T22:39:11.544+00:00`, inventory `...11.543+00:00` |
| F | Grafana datasources and dashboard | Verified | datasource health OK; UI showed request rate and 8 targets |
| G | Canary drill through Prometheus | Verified | 39x 200 and 1x 502; v0.2.0 5xx 54→55; held at 55 after rollback |
| H | Regression | Verified | 352 passed (292 unit + 60 integration) in 170.00s; none skipped |
| I | Documentation | Written | docs/OBSERVABILITY.md, TRACING.md, OBSERVABILITY_RUNBOOK.md |

### Known limitations of Milestone 4

- Telemetry stores are emptyDir. A restart drops metrics, logs, and traces.
- Prometheus, Loki, and Tempo have no authentication. Grafana allows anonymous viewers
  on ClusterIP. The admin password is a generated Secret and is not in git.
- Sampling in `platformforge-dev` is 100%.
- orders-service `destination_version` is `unknown` because the manifest sets no
  `spec.version`. Inventory versions are labeled.
- Tempo's Service still lists Jaeger ports. NetworkPolicy drops them; that was observed
  as timeouts to 9411 and 14268 from the plaintext test client.
- No metric-based promotion or automated rollback. The drill is still a manual manifest
  change, now visible in Prometheus.
- The Milestone 4 test run itself was on kind. The same stack was later installed on GKE; that evidence is Milestone 5.

## Milestone 5: GKE cluster with the platform installed

The cluster `platformforge-dev` in `us-central1-a` is running at
`v1.36.4-gke.1391002`. This sprint did not run `terraform apply` or
`terraform destroy`. It created one private Artifact Registry repository,
pushed `linux/amd64` images, and installed Istio, the applications, and
observability on the existing cluster. The kind cluster was not modified.
Evidence is in TEST_EVIDENCE.md. Operations are in
[docs/GKE_OPERATIONS.md](docs/GKE_OPERATIONS.md).

| Item | Status | Evidence |
| --- | --- | --- |
| Provider pin and modular root (VPC, subnet, NAT, firewall, IAM, zonal GKE, node pool, outputs) | Applied earlier by the operator; this sprint did not apply | `terraform fmt -check`, `init -backend=false`, `validate` |
| Private nodes, authorized control-plane networks, Workload Identity, least-privilege node IAM | Running | cluster describe; one Ready node, no external IP |
| Artifact Registry `platformforge` in `us-central1` | Private Docker repository. Node SA has repository-scoped `roles/artifactregistry.reader` | `gcloud artifacts repositories describe` and `get-iam-policy` |
| Images | Pushed `linux/amd64`, user `65532:65532`. Digests in gitignored `deploy/gke/image-lock.yaml` | `docker image inspect`, `gcloud artifacts docker images describe` |
| Istio 1.31.2 | Installed with the GKE operator file. istiod and CNI Ready. No gateway. Services are ClusterIP | `istioctl version`, `kubectl get pods,svc -n istio-system` |
| Applications | orders 0.2.0 and inventory 0.1.0 / 0.2.0 Ready, non-root, read-only root, native sidecars | rollout status and pod securityContext |
| STRICT mTLS and authorization | Enforced | destination `mutual_tls` plus `TLSv1.3` and orders SPIFFE on the inventory access log; default SA gets HTTP 403; plaintext is reset |
| Canary | Measured 180/20 on 200 requests, then restored to 100/0 | orders sidecar `istio_requests_total` |
| Observability | Prometheus, Grafana, Loki, Tempo, Alloy Ready. No PVCs. Grafana health OK for all three datasources | in-cluster queries |
| Incident drill | Faulty canary produced 11/100 orders HTTP 502 and inventory HTTP 500, then healthy 0 and stable 100/0 were restored. Recovery traffic was 30/30 HTTP 200 | Prometheus, Loki, Tempo |

### Known limitations of Milestone 5

- This sprint did not re-run `terraform plan` or `terraform apply`.
- The control-plane endpoint is public, limited to `master_authorized_cidrs`. A private
  endpoint is a variable, default off, because this environment has no bastion or VPN.
- Worker egress to the internet is allowed through Cloud NAT. There is no default-deny
  egress rule.
- VPC flow logs are off. Checkov CKV_GCP_26 reports that.
- Google Groups for GKE and Binary Authorization are not configured.
- The cluster resource has no `node_config` because the default pool is removed. The
  lasting node pool sets the GKE Metadata Server. Checkov CKV_GCP_69 fails on the
  cluster resource and passes on the node pool.
- One zone. The GKE free-tier credit covers the management fee only while this is the
  billing account's only zonal or Autopilot cluster. This cluster now occupies that slot.
- `make images` still defaults to `linux/arm64` for kind. GKE runs the amd64
  digests. `make images-gke-publish` still exits 2; the push in this sprint was
  a direct `docker push` after the repository existed.
- Local Terraform state until a private GCS bucket is approved.
- Service type LoadBalancer is not blocked by an admission controller. PlatformForge
  still refuses to render one. The GCE Ingress add-on is disabled.
- `platformforge deploy` still refuses the GKE context. Re-deploys are
  `gke.py render` plus `platformforge deploy --dry-run` plus `kubectl apply`
  with the pinned context.
- Client-side Istio metrics label `connection_security_policy` as `unknown`.
  mTLS evidence is the destination sidecar stat and the access log
  (`downstream_tls_version=TLSv1.3`).
- Telemetry disks are emptyDir. A Prometheus restart drops the drill counters.
  Loki and Tempo kept the drill log and trace through the verification window.

## Milestone 6: CI and progressive delivery, not applied live

GitHub Actions CI now validates Terraform, ServiceManifests, and dependency
pins, and scans built images. Canary promote/hold/rollback is a pure function
in `src/platformforge/delivery.py`. GitHub OIDC is Terraform with
`enable_github_delivery` defaulting to false. The delivery workflow refuses
`kubectl apply`. No image was pushed and no GKE object was changed for this
milestone. The cluster was read: it was `RUNNING` at `v1.36.4-gke.1391002`
with one node, and the inventory VirtualService was `v0-1-0=100 v0-2-0=0`.

| Item | Status | Evidence |
| --- | --- | --- |
| CI lint, tests, Terraform, manifests, secret scan, pip-audit, image scan config | Written | `make lint`, `make test` 335 passed, `terraform validate`, secret scan clean |
| GitHub OIDC and delivery IAM | Applied | State contains the 11 delivery resources. This pass did not apply again |
| Image publish workflow | One `0.2.0` publish succeeded | Run 39546044442. Cluster digests were not changed |
| Canary decision and idempotent rollback plan | Unit-tested | `tests/unit/test_delivery.py` |
| Live GKE promotion | Not run | `refuse-apply` exits 2 |

### Known limitations of Milestone 6

- The decision function has not been fed a live Prometheus response from this
  milestone. GKE routing was only read.
- The 11 GitHub delivery resources are in state. A plan after that apply
  reported no changes. This pass did not apply or destroy.
- The cluster IAM condition uses
  `projects/platformforge-dev-mn2026/zones/us-central1-a/clusters/platformforge-dev`.
  The publish run authenticated with that provider. It did not call kubectl.
- `MICHAELWlN/platformforge` is public. Environment `gke-delivery` requires
  reviewer `MICHAELWlN`, blocks admin bypass, and allows only `main`.
  `prevent_self_review` is false because that owner is the only reviewer.
  `GCP_DELIVERY_ENABLED` is `true`.
- The new `0.2.0` digests are in Artifact Registry. The running Deployments
  still use the previous digests.
- `deploy/gke/delivery-rbac.yaml` is not applied, so the delivery account
  cannot patch workloads even after OIDC exists.
- `main` has no branch protection. The environment policy is the ref gate.

## Milestone 7A: read-only MCP server, not connected live

`src/platformforge/mcp_server/` is a stdio MCP server on `mcp` 2.3.0. Twelve
tools list or read Kubernetes objects and loopback telemetry. Unit tests use
fakes. This pass did not start a long-running server, did not port-forward,
and did not call the GKE or kind API.

| Item | Status | Evidence |
| --- | --- | --- |
| stdio server and tool registration | Implemented | In-memory MCP client lists 12 tools |
| Context and namespace allowlists | Implemented | Rejection tests |
| Read-only Kubernetes wrapper | Implemented | Delete and Secret calls raise |
| Incident report | Implemented | `verified_root_cause` is null |
| Live GKE or kind use | Not run | No cluster command in this milestone |

### Known limitations of Milestone 7A

- The kubeconfig user is whatever the operator already has. The server does
  not narrow GKE IAM.
- Observability tools do nothing useful until the operator port-forwards to
  loopback. They were not pointed at the cluster.
- `istio-system` is outside the namespace allowlist.
- Log text can still contain a sentence that looks like an instruction. It is
  wrapped as untrusted data and is not executed.

## Milestone 7B: kind live reads

The reader ServiceAccount, Role, and RoleBinding in
`deploy/kind/mcp-reader.yaml` are applied on `kind-platformforge` only.
Live tool calls used a one-hour token for that account. Prometheus, Loki,
and Tempo were reached through `127.0.0.1` port-forwards. GKE was not called.
The current kubeconfig context stayed
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`.

| Item | Status | Evidence |
| --- | --- | --- |
| Reader RBAC | Applied on kind | SelfSubjectAccessReview in `tests/integration/test_mcp_live.py` |
| Twelve MCP tools | Passed against kind | Same test, in-process MCP client |
| Incident report | `verified_root_cause` null | Retained `istio_requests_total` includes HTTP 500 and 503 |
| Tempo traces | None retained | Search returned no traces. A lookup reported the id missing |
| GKE | Not used | Context was displayed and left unchanged |

### Known limitations of Milestone 7B

- Kind images are tags, so `get_deployment_status` reports `immutable: false`.
- The 15-minute Loki window contained three `istio-proxy` lines and no
  `inventory-service` container lines. No request id was available to join
  to the Prometheus counters.
- Tempo's disk is emptyDir. No trace was available to correlate.
- The reader cannot read `platformforge-observability` or `istio-system`.
  Port-forwards are an operator action.
- GKE still has no reader ServiceAccount.

## Milestone 7C: GKE live reads

`deploy/gke/mcp-reader.yaml` is applied on the pinned GKE context only.
Live tool calls used a one-hour token for that account. GKE calls without
`PLATFORMFORGE_MCP_KUBECONFIG` are rejected. Prometheus, Loki, and Tempo were
reached through `127.0.0.1` port-forwards. The kind cluster was not contacted.
Routing stayed `v0-1-0=100`, `v0-2-0=0`.

| Item | Status | Evidence |
| --- | --- | --- |
| Reader RBAC | Applied on GKE | SelfSubjectAccessReview in `tests/integration/test_mcp_gke.py` |
| Twelve MCP tools | Passed against GKE | Same test, in-process MCP client, 2 passed |
| Canary image | Digest-pinned | `sha256:e995d076aa876ac8c8d1b854e9364256b83a52162a871c63bfefffb28778abe4` |
| Incident report | `verified_root_cause` null | Prometheus still has 26 HTTP 500 samples |
| Tempo and inventory logs | Not in the 15-minute window | Reported as missing, not as a healthy zero |

### Known limitations of Milestone 7C

- The operator client still mints the token and opens the port-forwards.
- No request id or trace id joins the retained HTTP 500 counter to one span.
- The reader cannot list `platformforge-observability` or `istio-system`.

## Milestone 8: canary analysis and kind rollback

`platformforge.canary` compares stable and canary Prometheus samples and
returns promote, hold, or rollback. `platformforge.canary_apply` replaces the
inventory VirtualService from the reviewed 100/0 or 90/10 file. It accepts
only `kind-platformforge` and `platformforge-dev`. The stable Deployment is
not deleted. GitHub Actions was not changed.

| Item | Status | Evidence |
| --- | --- | --- |
| Thresholds, parsing, timeouts, transitions | Verified | `tests/unit/test_canary.py` |
| Kind failure drill | Verified | `tests/integration/test_canary_kind.py`, 1 passed in 28.68s |
| Canary 5xx ratio in the decision | 1.0 on `v0.2.0` | 60s `increase` of `istio_requests_total` |
| Route recovery | 1.158s to sidecar weight 100 | Then 30/30 HTTP 200 while the canary was still faulty |
| Healthy canary restored | `INVENTORY_FAILURE_PERCENT=0`, weights 100/0 | `finally` in the same test |
| Grafana panels | Kind ConfigMap updated | GKE ConfigMap was not changed |
| In-process canary counters | Rendered as text | Not scraped by cluster Prometheus |

### Known limitations of the one-shot drill

- The live decision's p95 fields were null. A later 300s histogram query
  computed p95, and that query was not the rollback input.
- GKE routing was not analyzed or changed.

## Milestone 8 controller: kind reconciliation loop

`platformforge.canary_controller` polls on `kind-platformforge`, compares
counter deltas since a baseline, and applies the reviewed stable or promote
VirtualService. It serves `GET /metrics`. Prometheus job `platformforge-canary`
scrapes that ClusterIP. This section records the kind loop. The GKE
Deployment is the digest-pinned manifest in `deploy/gke/canary-controller.yaml`.

| Item | Status | Evidence |
| --- | --- | --- |
| State machine, baseline, shutdown, metrics port | Verified | `tests/unit/test_canary_controller.py` |
| Five kind experiments | Verified | 1 passed in 335.84s. See `docs/CANARY_CONTROLLER.md` |
| Healthy 90/10 | promote | 400 × HTTP 200, canary total 56, p95 regression 0.031, then sidecars at `v0-2-0` weight 100 |
| Faulty canary | rollback, error ratio 1.0 | 52 × HTTP 502 and 348 × HTTP 200 during the shift; next 20 × HTTP 200 |
| 1000 ms delay, no HTTP failures | rollback, canary p95 975 ms | 220 × HTTP 200, error ratio 0, regression 1025.32; next 20 × HTTP 200 |
| Prometheus URL `http://127.0.0.1:9` | fail closed, no promote | sidecars back to 100/0 in 129.4 s; URL restored |
| Repeated stable apply | no second route write | stable Deployment ready 2; SA delete denied |
| Scrape | Series changed during the run | `rollbacks_total` 2→3→4, then 0→1 after the pod replacement |
| Grafana | Kind and GKE ConfigMaps have the controller panels | GKE API: health ok, three datasources healthy, dashboard `platformforge-overview` listed. UI was not opened |

### Known limitations of the controller

- `PLATFORMFORGE_CANARY_CONTEXT` is an environment check, not the API server identity. On GKE the process also requires `PLATFORMFORGE_CANARY_GKE=1` and the pod token. The Role is the write boundary.
- Counters reset when the pod is replaced.
- The 90/10 file is applied by the operator. The controller does not select it.
- GKE controller image `sha256:b7e8af0a35fd1d968ee3aacf701701e15da8ff600d6360662885f6c5846cc6a2` is running. The first rollback is in `docs/GKE_CANARY_EXPERIMENT.md`. A later drill raised the cumulative rollback counter from 1 to 2. See `TEST_EVIDENCE.md`. Terraform and GitHub Actions were not changed.
- The GKE latency drill is not measured. The running inventory digest does not set `INVENTORY_DELAY_MS`.

## Later milestones

| Area | Status |
| --- | --- |
| Terraform (VPC, Cloud NAT, zonal GKE, Workload Identity, node IAM) | **Cluster running.** This sprint did not apply or destroy |
| GKE workload install (images, Istio, observability, apps) | **Installed** on the existing cluster. CLI still refuses the GKE context |
| Artifact Registry | **One private repository** `platformforge` in `us-central1` |
| Istio (STRICT mTLS, traffic management, canary) | **Verified on kind and on this GKE cluster** |
| OpenTelemetry, Prometheus, Loki, Tempo, Grafana | **Verified on kind and on this GKE cluster** |
| CD (publish images, deploy from CI) | **Publish verified.** One `0.2.0` push succeeded. Cluster deploy from CI is still refused |
| Read-only MCP server | **Cursor called the GKE tools**, including the three rejection checks. The GKE reader Role now grants `get` on `virtualservices/inventory-service` only. `get_routing` returned `v0-1-0=100` and `v0-2-0=0`. See `docs/FINAL_DEMO.md` |
| Incident simulation and rollback | Kind controller executed promote, error rollback, latency rollback, and fail-closed rollback. On GKE the cumulative rollback counter moved from 1 to 2 after a later faulty-canary drill. `verified_root_cause` stays null |
| Recording port-forwards | **Local supervisor running.** `make demo-connect` restarts a dead loopback forward. `make demo-forwards` is still one-shot. No cluster object was changed by this pass |
