# Architecture

This document describes what exists today (Milestone 1) and, separately, the target
architecture. Only the first section is implemented.

## Milestone 1: implemented

```
 developer
    |
    v
 platformforge CLI (Typer)             src/platformforge/cli.py
    |
    |-- validate --------> manifest.py: YAML (safe, no aliases, <=64 KiB)
    |                        -> Pydantic ServiceManifest (strict, extra=forbid)
    |                        -> namespace allowlist, name/image/resource/probe policy
    |
    |-- deploy --dry-run -> resources.py: pure builders -> YAML to stdout (no cluster)
    |
    |-- deploy/status/logs
            |
            v
       kubecontext.py: read kubeconfig YAML -> display context -> require approval
            |            (interactive y/N, or --approve-context NAME == current context)
            v
       kubernetes client pinned to approved context name
            |
            v
       deployer.py: allowlist re-check -> ownership preflight -> create or replace
            |        -> poll Deployment status until rollout complete
            v
       Kubernetes API (kind-platformforge)  ->  namespace platformforge-dev
                                                 |- Deployment demo-web (2 replicas)
                                                 |- Service demo-web (ClusterIP)
```

### Modules

| Module | Responsibility | Talks to cluster |
| --- | --- | --- |
| `manifest.py` | Schema, allowlist, quantity parsing, YAML loading | No |
| `resources.py` | Render Deployment / Service / Namespace objects | No |
| `kubecontext.py` | Show current context, approval, build API clients | Builds clients only |
| `deployer.py` | Create/replace, ownership checks, rollout wait, status, logs | Yes |
| `cli.py` | Commands, output, exit codes | Via the above |

### Design decisions

- **Own manifest format, not Kubernetes YAML.** Developers cannot pass arbitrary Kubernetes
  objects. The platform decides security context, Service type, labels, and rollout strategy.
- **Strict Pydantic models** (`strict=True`, `extra="forbid"`, frozen). Type coercion is off
  (e.g. `replicas: "2"` is rejected); CPU accepts a YAML int as a convenience.
- **Allowlist in code.** `ALLOWED_NAMESPACES = {"platformforge-dev"}`. Not configurable by
  environment variable or flag, so it cannot be widened without a reviewed change.
- **Defense in depth.** The allowlist is checked at manifest load and again at every
  `deployer` entry point.
- **Ownership label.** Existing objects without `app.kubernetes.io/managed-by=platformforge`
  are never replaced. Ownership is checked for both Deployment and Service *before* any
  write, and re-checked on 409 conflict to cover races.
- **Create-or-replace instead of server-side apply.** Replace gives full-object semantics
  (removed env vars are actually removed). Service `clusterIP(s)` are carried over because
  they are immutable. Server-side apply is a candidate for a later milestone.
- **Context display before credential loading.** `kubecontext.current_context()` parses
  kubeconfig YAML itself so the API server URL is shown before any exec credential plugin
  (e.g. `gke-gcloud-auth-plugin`) runs. The client is then built for that exact context name,
  so a concurrent `kubectl config use-context` cannot redirect the operation.
- **No shell-outs.** All cluster access is through the official `kubernetes` Python client.

### Rollout completion

A rollout is complete when `status.observedGeneration >= metadata.generation` and
`updatedReplicas == availableReplicas == replicas == spec.replicas`. This mirrors the
conditions `kubectl rollout status` uses, including waiting for old replicas to terminate.

## Target architecture: not yet implemented

The following are planned and **do not exist in the repository yet**:

- Terraform-managed GCP: VPC, GKE (private nodes, Workload Identity), Artifact Registry
- GKE workload deployment through the same CLI
- Istio with STRICT mTLS, traffic management, and canary deployments
- `orders-service` and `inventory-service` FastAPI microservices
- OpenTelemetry instrumentation; Prometheus, Loki, Tempo, Grafana
- GitHub Actions CI/CD
- Read-only MCP server for Kubernetes diagnostics
- Controlled incident simulation, AI-assisted investigation, rollback
