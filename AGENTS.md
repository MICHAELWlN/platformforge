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
10. Do not begin a later milestone (GKE, Istio, observability, MCP) unless asked.

## Layout

```
src/platformforge/
  manifest.py     Pydantic schema, allowlist, YAML loading (no cluster access)
  resources.py    Pure builders: manifest -> V1Deployment / V1Service / V1Namespace
  kubecontext.py  Context discovery, display, approval, client construction
  deployer.py     Create/replace with ownership checks, rollout wait, status, logs
  cli.py          Typer CLI: validate, deploy, status, logs
examples/         Example ServiceManifest(s)
tests/unit/       Fast tests; Kubernetes API is mocked
tests/integration Opt-in tests against a local kind cluster
```

## Commands

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                   # unit tests
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
PLATFORMFORGE_IT_CONTEXT=kind-platformforge .venv/bin/pytest -m integration -v
```

## Conventions

- Every new manifest field must be explicit, bounded, and covered by a rejection test.
- Every API entry point in `deployer.py` must re-check the namespace allowlist and, for
  existing objects, the `app.kubernetes.io/managed-by=platformforge` label.
- Note: kubernetes client v37 models are Pydantic-based; attribute names are snake_case
  (e.g. `spec.external_ips`, not the older `external_i_ps`).
