# Istio service mesh (Milestone 3)

PlatformForge workloads in `platformforge-dev` run in an Istio sidecar mesh on the local kind
cluster. This document records the version decision, exact installation commands, what was
configured, how each property was verified, known limitations, and cleanup.

Scope: **local kind cluster only** (`kind-platformforge`, single node, arm64). Nothing here
has been run on GKE or any cloud, and none of it is production-validated.

## Version selection

| Item | Value | Source |
| --- | --- | --- |
| Kubernetes (server) | v1.37.0 | `kubectl version` |
| Istio | **1.31.2** | [Supported releases](https://istio.io/latest/docs/releases/supported-releases/) (checked 2026-10-20) |
| Istio 1.31 supported Kubernetes | 1.32, 1.33, 1.34, 1.35, 1.36, **1.37** | same page |
| Istio 1.30 supported Kubernetes | 1.32–1.36 (**not** 1.37) | same page |
| Istio 1.31 patches without known CVEs | 1.31.2+ | same page |
| Envoy | release/v1.39 | same page |
| `istioctl` artifact | `istio-1.31.2-osx-arm64.tar.gz`, SHA-256 `c02f9390ff8300b550c7b13230feae80adebe31aa7136f2930a1918502c626d3` | GitHub release asset `.sha256`, verified locally |

1.31 is the only supported minor release whose support matrix includes Kubernetes 1.37;
1.31.2 is its latest patch (released 2026-10-16) and the first without known CVEs. The
`master` development build was not considered.

## Design decisions

- **Sidecar mode with Istio CNI.** `platformforge-dev` enforces the `restricted` Pod
  Security level. The default `istio-init` container needs `NET_ADMIN`/`NET_RAW`, which
  `restricted` forbids; the Istio CNI node agent configures pod networking instead
  ([Istio PSA guide](https://istio.io/latest/docs/setup/additional-setup/pod-security-admission/)).
  The CNI DaemonSet needs `hostPath`, so `istio-system` is labeled `privileged`.
  `platformforge-dev` remains `restricted`.
- **Native sidecars** (Istio default on Kubernetes ≥ 1.33): `istio-proxy` is an init
  container with `restartPolicy: Always`, so it starts before and stops after the app.
- **`minimal` profile**: CRDs + istiod only. No ingress or egress gateway, so nothing is
  exposed outside the cluster.
- **Namespace-scoped policy.** Injection is enabled only on `platformforge-dev`; the
  PeerAuthentication, AuthorizationPolicy, DestinationRule, and VirtualService all live in
  `platformforge-dev`. No mesh-wide policy is created in `istio-system`.
- **Evidence-oriented telemetry.** JSON Envoy access logs include the TLS version and the
  peer SPIFFE identities on both ends of each hop. Tracing is disabled (a later milestone),
  which also keeps Envoy from rewriting application `X-Request-ID` values.
- **Kind-sized resources.** istiod requests 100m/256Mi (default 500m/2Gi), sidecars
  20m/64Mi with 500m/256Mi limits (default 100m/128Mi, 2 CPU/1Gi).

Configuration files:

| File | Purpose |
| --- | --- |
| `deploy/istio/istio-system-namespace.yaml` | `istio-system` with PSA `privileged` (CNI DaemonSet) |
| `deploy/istio/istio-operator.yaml` | IstioOperator install configuration |
| `deploy/istio/platformforge-dev/peer-authentication.yaml` | STRICT mTLS, namespace-wide |
| `deploy/istio/platformforge-dev/inventory-authorization.yaml` | inventory admits only orders' identity, `GET /inventory/*` |
| `deploy/istio/platformforge-dev/inventory-destinationrule.yaml` | subsets `v0-1-0`, `v0-2-0` |
| `deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml` | 100% → v0.1.0 (default / rollback) |
| `deploy/istio/platformforge-dev/inventory-virtualservice-canary.yaml` | 90% / 10% |

These are reviewed infrastructure files applied with `kubectl`. They are deliberately
**not** PlatformForge manifests: the CLI does not accept arbitrary Kubernetes or Istio objects.

## Installation (exact commands)

```bash
# 1. istioctl into a gitignored tools dir, with checksum verification
mkdir -p .tools && cd .tools
V=1.31.2; F=istio-$V-osx-arm64.tar.gz
curl -fsSLO https://github.com/istio/istio/releases/download/$V/$F
curl -fsSLO https://github.com/istio/istio/releases/download/$V/$F.sha256
[ "$(awk '{print $1}' $F.sha256)" = "$(shasum -a 256 $F | awk '{print $1}')" ] && echo OK
tar -xzf $F && cd ..
export PATH="$PWD/.tools/istio-$V/bin:$PATH"

# 2. Review what will be installed (offline), then pre-check the cluster
istioctl manifest generate -f deploy/istio/istio-operator.yaml > /tmp/istio-rendered.yaml
kubectl config current-context        # must be kind-platformforge
istioctl x precheck --context kind-platformforge

# 3. Install
kubectl --context kind-platformforge apply -f deploy/istio/istio-system-namespace.yaml
istioctl install -f deploy/istio/istio-operator.yaml --context kind-platformforge -y

# 4. Enable injection for platformforge-dev only, then restart its workloads one at a time
kubectl --context kind-platformforge label namespace platformforge-dev istio-injection=enabled
for d in inventory-service orders-service demo-web; do
  kubectl --context kind-platformforge -n platformforge-dev rollout restart deploy/$d
  kubectl --context kind-platformforge -n platformforge-dev rollout status deploy/$d --timeout=180s
done

# 5. Mesh policy (namespace-scoped)
kubectl --context kind-platformforge apply -f deploy/istio/platformforge-dev/peer-authentication.yaml
kubectl --context kind-platformforge apply -f deploy/istio/platformforge-dev/inventory-authorization.yaml
kubectl --context kind-platformforge apply -f deploy/istio/platformforge-dev/inventory-destinationrule.yaml
kubectl --context kind-platformforge apply -f deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml
```

Step 4 is only needed once, for workloads created before injection was enabled. A namespace
created by `platformforge deploy --create-namespace` now carries `istio-injection=enabled`.
`kubectl rollout restart` adds a `restartedAt` annotation that the next `platformforge
deploy` removes, which triggers one more (harmless) rollout.

The `minimal` rendered manifest was reviewed before install: 15 CRDs, 5 ClusterRoles/Bindings,
one ClusterIP Service (`istiod`), one mutating and one validating webhook. The injection
webhooks only match namespaces labeled `istio-injection=enabled`/`istio.io/rev`, or pods
labeled `sidecar.istio.io/inject=true`.

Note: `istioctl verify-install` no longer exists in 1.31. Health was verified from workload
status, the CNI agent log, and `istioctl proxy-status`.

## What was verified (summary)

Full commands and output are in [TEST_EVIDENCE.md](../TEST_EVIDENCE.md) (section M3). The
automated checks are in `tests/integration/test_services_mesh.py`.

| Property | Evidence |
| --- | --- |
| Control plane healthy | istiod 1/1, istio-cni-node 1/1, 0 restarts; control plane version 1.31.2 |
| CNI chained into kindnet | `/etc/cni/net.d/10-kindnet.conflist` plugins `[ptp, portmap, istio-cni]` |
| Sidecars injected, PSA `restricted` honored | native `istio-proxy` (UID 1337, drop ALL, read-only root, no privilege escalation); no `istio-init`; server-side PSA dry-run reported no violations; no `FailedCreate` events |
| Proxies synced | `istioctl proxy-status`: all sidecars subscribed to istiod (CDS/LDS/EDS/RDS) |
| Workload identities | Envoy certificates carry `spiffe://cluster.local/ns/platformforge-dev/sa/{orders-service,inventory-service}` |
| No extra Kubernetes API permissions | SubjectAccessReview denies get/list/create on pods, secrets, configmaps, services, AuthorizationPolicies; no RoleBinding or ClusterRoleBinding references the ServiceAccounts; no Kubernetes API token is mounted (only Istio's `istio-ca` audience token) |
| STRICT mTLS | control: before STRICT, plaintext from a non-mesh pod got `HTTP/1.1 200`. After STRICT, TCP connects and is then reset, for every orders/inventory pod and via both Services |
| Traffic is actually encrypted | orders' outbound and inventory's inbound access-log entries for the same request ID both show `TLSv1.3` and the peer's SPIFFE ID; inventory's `istio_requests_total` reports `connection_security_policy=mutual_tls` for orders traffic |
| Authorization | orders identity: `GET /inventory/*` 200. Other mesh identity (`sa/default`): 403 `RBAC: access denied`, logged with that peer identity. orders identity on `/version`, `/healthz`, POST, DELETE: 403. orders-service (not covered by the policy) still reachable |
| Health checks preserved | probes rewritten to `:15020/app-health/<container>/{readyz,livez}`; pods Ready, 0 restarts; `platformforge status` HEALTHY |

## How kubelet probes work under the mesh

Istio rewrites each `httpGet` probe to the sidecar agent (`port 15020`,
`/app-health/<container>/readyz`), which forwards it to the app over localhost. Probes
therefore neither need mTLS nor pass through the inventory AuthorizationPolicy. The original
`kube-probe` User-Agent is preserved (verified by the demo-web log test).

## Behavior changes caused by the mesh

- **The Kubernetes API server's service proxy no longer reaches the services.** It is a
  plaintext, non-mesh client. The M2 integration tests now issue requests from inside the
  cluster (see `tests/integration/mesh.py`).
- **Upstream failures are reported by the caller's sidecar.** When inventory has no healthy
  endpoint, orders' sidecar answers `503 no_healthy_upstream` (flag `UH`) instead of the
  connection error the app saw before. orders-service v0.2.0 maps upstream 503 → 503
  `inventory_unavailable`, 504 → 504 `inventory_timeout`, 403 → 502 `inventory_forbidden`.
- **Direct pod-IP calls from inside the mesh are plaintext.** A sidecar does not know a raw
  pod IP, so it forwards it via `PassthroughCluster` without mTLS. STRICT rejects it (`503`,
  flag `UC`). Callers must use Service names.

## Limitations

- Single-node kind cluster; no multi-node, multi-cluster, or cloud validation.
- Istio's self-signed root CA (istiod's default) is used; no external CA or rotation drills.
  Workload certificates are 24-hour certificates rotated by istiod.
- Envoy's TLS handshake statistics are not exported (Istio trims Envoy stats by default); TLS
  evidence comes from access logs and Istio's standard metrics.
- Only inventory-service has an AuthorizationPolicy. orders-service and demo-web accept any
  mTLS peer in the mesh (STRICT still rejects plaintext).
- No egress control (`outboundTrafficPolicy` is the default `ALLOW_ANY`) and no
  NetworkPolicy. A pod that opts out of injection is still subject to STRICT on the
  server side, but it is not blocked from making plaintext calls to non-mesh destinations.
- No Prometheus, Kiali, Grafana, or tracing (later milestone); metrics were read directly
  from Envoy.
- The CNI plugin is chained into the node's kindnet config, so it runs for every new pod on
  the node (it only acts on pods with injected sidecars; `kube-system` is excluded).

## Cleanup (manual, destructive; requires explicit approval)

Remove mesh policy, then injection, then Istio. Review each command before running it:

```bash
kubectl --context kind-platformforge -n platformforge-dev delete \
  virtualservice/inventory-service destinationrule/inventory-service \
  authorizationpolicy/inventory-service peerauthentication/default
kubectl --context kind-platformforge label namespace platformforge-dev istio-injection-
kubectl --context kind-platformforge -n platformforge-dev rollout restart deploy   # drop sidecars
istioctl uninstall --purge --context kind-platformforge -y
kubectl --context kind-platformforge delete namespace istio-system
```

The test client pods (`pf-test-client-mesh`, `pf-test-client-plain`) and the
`inventory-service-v0-2-0` canary are removed with
`kubectl -n platformforge-dev delete pod/<name>` and `delete deployment/inventory-service-v0-2-0`.
