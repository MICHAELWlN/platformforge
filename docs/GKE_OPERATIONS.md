# GKE operations

The running platform on cluster `platformforge-dev` in project
`platformforge-dev-mn2026`, zone `us-central1-a`. Context:

`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`

`kubectl` from Docker needs `gke-gcloud-auth-plugin` on `PATH`. On this
machine that is `/opt/homebrew/share/google-cloud-sdk/bin`. Display the
current context before every cluster command. `platformforge deploy` refuses
this context. Render with `python -m platformforge.gke render`, print
Kubernetes YAML with `platformforge deploy --dry-run`, then `kubectl apply
--context` the pinned context.

## What is installed

| Piece | Where | Notes |
| --- | --- | --- |
| Kubernetes | `v1.36.4-gke.1391002` | One Ready `e2-standard-4`, `linux/amd64`, no external IP |
| Registry | `us-central1-docker.pkg.dev/platformforge-dev-mn2026/platformforge` | Private. Node SA has repository `roles/artifactregistry.reader` only |
| Istio | 1.31.2, minimal, CNI, native sidecars | `deploy/gke/istio-operator.yaml`. No ingress or egress gateway |
| Apps | `platformforge-dev` | Digest images, ClusterIP, restricted Pod Security, injection on |
| Mesh | same namespace | STRICT, inventory allow `orders-service`, stable 100/0 |
| Telemetry | `platformforge-observability` | Injection off. ClusterIP. emptyDir only. No PVCs |

Deployed digests (also in gitignored `deploy/gke/image-lock.yaml`):

| Image | Digest |
| --- | --- |
| inventory-service 0.1.0 | `sha256:a888c1bf6ab6c8e5a57d28ee010ffe206296e7b85a3f7dc58f7463b16a9f00ba` |
| inventory-service 0.2.0 | `sha256:e995d076aa876ac8c8d1b854e9364256b83a52162a871c63bfefffb28778abe4` |
| orders-service 0.2.0 | `sha256:8058e9e6934196bfc44749b579613b2d5225c787c1c710c0c8f624e57b643ac5` |

After install, `kubectl top nodes` showed 627m CPU (15%) and 3028Mi (22%) on
the allocatable 3920m / 13273Mi node. Do not add a node. If a pod stays
Pending, stop and ask.

## Security controls that stay in place

- PeerAuthentication `default` in `platformforge-dev` is STRICT.
- AuthorizationPolicy `inventory-service` allows only
  `cluster.local/ns/platformforge-dev/sa/orders-service` to `GET /inventory/*`.
- Application containers are non-root, read-only root, and drop all
  capabilities. Istio's sidecar runs as UID 1337 with the same restrictions.
  The CNI DaemonSet is privileged because it chains into
  `/home/kubernetes/bin` and `/etc/cni/net.d` beside GKE Dataplane V2. It does
  not replace Cilium.
- No LoadBalancer, NodePort, or Ingress. Grafana anonymous auth is off. The
  admin password is the chart Secret. Do not print it or commit it.
- Workload Identity ServiceAccount `platformforge-gcp` exists and is not the
  identity of orders or inventory.

Prove mTLS from an inventory sidecar access log (`downstream_tls_version` and
`downstream_peer_uri_san`) or from destination `istio_requests_total`
`connection_security_policy=mutual_tls`. A HTTP 200 alone is not that proof.
Client-side stats on this cluster label the policy `unknown`.

## Checks

Kind integration stays on `kind-*`. GKE checks:

```bash
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:$PATH"
export PLATFORMFORGE_GKE_CONTEXT=gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev
.venv/bin/pytest tests/gke -m gke -q
```

`make test` skips those four tests. On 2026-10-20 it reported 308 passed and
64 skipped. The opted-in GKE run reported 4 passed.

## Cost

The new billable pieces beyond the existing node, NAT, and control plane are
Artifact Registry storage and NAT egress for image pulls. There is no extra
disk, load balancer, or node. The $25 budget is an alert, not a cap. List
prices for the node and the management fee are in [CLOUD_COSTS.md](CLOUD_COSTS.md).

## Cleanup

Do not `terraform destroy` and do not delete the kind cluster. To remove only
the platform from GKE, with approval:

1. Helm uninstall `grafana`, `alloy`, `prometheus`, `loki`, and `tempo` in
   `platformforge-observability` using the pinned context.
2. Delete the PlatformForge namespaces `platformforge-dev`,
   `platformforge-observability`, and `istio-system` only after confirming
   those are the namespaces this install created.
3. Leave the GKE node pool, VPC, and firewall rules alone.
4. Delete the Artifact Registry repository only with a separate approval.
   `gcloud artifacts repositories delete` is not part of routine cleanup.

Deleting `istio-system` removes the mesh. Do that only when the demo is
finished. `deletion_protection` on the cluster blocks `terraform destroy`.
