# MCP security

The server is an investigation aid. Its safety comes from what the code
refuses to call, not from a new GCP role.

## Controls

- stdio only. `run(transport="stdio")` is the only entry point. Setting
  `PLATFORMFORGE_MCP_HTTP` aborts startup.
- Context allowlist: `kind-platformforge` and the pinned GKE context. The
  current kubeconfig context is not used implicitly.
- Namespace allowlist: `platformforge-dev` and `platformforge-observability`.
  `istio-system`, `kube-system`, and every other namespace are rejected.
- `ReadOnlyApi` refuses method names that contain create, delete, patch,
  replace, update, exec, attach, proxy, secret, or token.
- Kubernetes Secrets are never read. Env names that look like tokens,
  passwords, or keys are returned as `[redacted]`. Values from `valueFrom`
  are not read.
- Log and query text is redacted for bearer tokens, private keys, GitHub
  tokens, AWS access-key ids, and JWTs, then wrapped as `untrusted` with an
  instruction not to follow text inside it.
- PromQL and LogQL must match a fixed character set, stay under 400 characters,
  and Loki must include a `{selector}`. The time window is at most 15 minutes.
- Telemetry URLs must be loopback HTTP with no userinfo. Responses are capped.
- Each Kubernetes and HTTP call uses a 5 second timeout.
- Tool results are truncated. The server does not spawn a shell and does not
  take a filesystem path from the model.

## Authentication assumption

Kind and GKE live checks each set `PLATFORMFORGE_MCP_KUBECONFIG` to their own
one-hour token for `platformforge-mcp-reader`. The tokens are not
interchangeable. The server accepts the file only when the user is
`platformforge-mcp-reader` and the context is `kind-platformforge` or the
pinned GKE context. A GKE call without the file is rejected and does not use
the operator kubeconfig. Kind without the file still uses the kubeconfig user
for `kind-platformforge`, which is cluster-admin. The kind live suite does
not take that path.

The Roles are `deploy/kind/mcp-reader.yaml` and `deploy/gke/mcp-reader.yaml`.
Neither is a GCP IAM binding.

## What this does not prove

- That a kubeconfig plugin cannot be invoked when a client is constructed.
  `load_clients` loads the named context. Tools do that only after the
  allowlist check.
- That logs cannot contain a persuasive sentence. They can. The report keeps
  that sentence in untrusted evidence and still sets `verified_root_cause`
  to null.
- That the operator identity used to mint the token is itself read-only.
  Minting and port-forwards use the operator client. Tool calls use the token.
  The GKE results are in [MCP_GKE_INTEGRATION.md](MCP_GKE_INTEGRATION.md).
