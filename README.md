# PlatformForge

PlatformForge is an internal developer platform (IDP) portfolio project. Developers describe a
service in a small, validated YAML manifest; the `platformforge` CLI renders it into hardened
Kubernetes resources and deploys it into an allowlisted namespace.

> **Status: Milestone 1 (local CLI + Kubernetes deployment engine).** Verified against a local
> kind cluster only. Nothing here is production-ready, and GKE, Terraform, Istio, observability,
> CI/CD, and MCP are **not implemented yet**. See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## Quick start

Requirements: Python 3.11+ (developed on 3.13), and for cluster commands a kubeconfig context
(developed against kind v0.33 / Kubernetes v1.37).

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
source .venv/bin/activate

platformforge validate examples/demo-web.yaml
platformforge deploy examples/demo-web.yaml --dry-run          # render only, no cluster access
platformforge deploy examples/demo-web.yaml --create-namespace  # prompts to approve kube context
platformforge status examples/demo-web.yaml
platformforge logs   examples/demo-web.yaml --tail 50
```

Every command that talks to a cluster first prints the current kube context (name, cluster,
API server URL, user, kubeconfig path) and asks for confirmation. For scripts, pass
`--approve-context <name>`; it must exactly match the current context or the command aborts.

## Commands

| Command | Cluster access | Purpose |
| --- | --- | --- |
| `validate MANIFEST` | none | Schema + policy validation |
| `deploy MANIFEST` | read/write, one namespace | Create or update Deployment + ClusterIP Service, wait for rollout |
| `status MANIFEST` | read-only | Deployment replicas/conditions, Service, Pods, health |
| `logs MANIFEST` | read-only | Last `--tail` lines (1–5000) from each pod |

`deploy` options: `--create-namespace`, `--wait/--no-wait` (default wait), `--timeout 10..900`
(default 120 s), `--dry-run`, `--approve-context NAME`.

Exit codes: `0` success, `1` invalid manifest, `2` CLI usage error, `3` kube context missing or
not approved, `4` Kubernetes API / ownership / not-deployed error, `5` rollout timeout.

## Service manifest

```yaml
apiVersion: platformforge.dev/v1alpha1
kind: ServiceManifest
metadata:
  name: demo-web                 # DNS-1035 label, <= 63 chars
  namespace: platformforge-dev   # must be in the code-level allowlist
spec:
  image: nginxinc/nginx-unprivileged:1.29-alpine   # explicit tag or @sha256 digest; no :latest
  replicas: 2                    # 1..5
  port: 8080                     # 1024..65535 (non-root)
  env:                           # plain values only; credential-like names are rejected
    - name: LOG_LEVEL
      value: info
  resources:                     # required; requests <= limits; limits <= 2 CPU / 2Gi
    requests: {cpu: 50m, memory: 32Mi}
    limits: {cpu: 200m, memory: 64Mi}
  probes:                        # both required; HTTP GET on the service port
    readiness: {path: /, initialDelaySeconds: 2, periodSeconds: 5}
    liveness: {path: /, initialDelaySeconds: 10, periodSeconds: 10}
```

Unknown fields anywhere are rejected, so raw Kubernetes objects (or fields such as
`hostNetwork`, `securityContext`, `valueFrom`, `type: LoadBalancer`) cannot be smuggled in.

## What gets deployed

- **Namespace** (only with `--create-namespace`): labeled
  `pod-security.kubernetes.io/enforce=restricted`.
- **Deployment**: rolling update (`maxSurge: 1`, `maxUnavailable: 0`), readiness + liveness
  probes, requests/limits, `runAsNonRoot`, `seccompProfile: RuntimeDefault`,
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, all capabilities dropped,
  no service-account token automount, `/tmp` as a 64Mi `emptyDir`.
- **Service**: `ClusterIP` only. PlatformForge never renders NodePort, LoadBalancer, Ingress, or
  external IPs.

All objects carry `app.kubernetes.io/managed-by=platformforge`. PlatformForge refuses to
update (or report on) an existing Deployment/Service of the same name that lacks this label.

## Testing

```bash
pytest                                   # unit tests; integration tests are skipped
ruff check src tests && ruff format --check src tests

# Integration tests: kind contexts only, current context must match, nothing is deleted.
PLATFORMFORGE_IT_CONTEXT=kind-platformforge pytest -m integration -v
```

Recorded results are in [TEST_EVIDENCE.md](TEST_EVIDENCE.md).

## Cleanup (manual, destructive)

Integration tests intentionally leave `platformforge-dev` in place. To remove everything
PlatformForge created on the local kind cluster, review and run:

```bash
kubectl --context kind-platformforge delete namespace platformforge-dev
```

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): components, data flow, trust boundaries
- [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md): what is and is not implemented
- [TEST_EVIDENCE.md](TEST_EVIDENCE.md): commands run and their results
- [AGENTS.md](AGENTS.md): rules for AI coding agents working in this repository
