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
| 10 | Unit and integration tests | Verified | 135 unit + 10 integration passing |
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

## Later milestones

| Area | Status |
| --- | --- |
| Terraform (GCP network, GKE, Artifact Registry) | Not started |
| GKE workload deployment | Not started |
| Istio (STRICT mTLS, traffic management, canary) | Not started |
| orders-service / inventory-service (FastAPI) | Not started |
| OpenTelemetry, Prometheus, Loki, Tempo, Grafana | Not started |
| GitHub Actions CI/CD | Not started |
| Read-only MCP server | Not started |
| Incident simulation and rollback | Not started |
