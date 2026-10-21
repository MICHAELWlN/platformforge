# GKE canary experiment

The controller is running on the pinned cluster. One faulty-canary rollback
was measured on 2026-10-21 and the healthy canary was restored. The procedure
below is what was run. It is not a standing approval to repeat the failure.

Pinned context, used on every command:

`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`

Project `platformforge-dev-mn2026`, cluster `platformforge-dev`, zone
`us-central1-a`. Do not change the kubeconfig current context if it is already
that context. Do not point these commands at kind.

## Preflight, before the controller existed

Read-only, on 2026-10-21, before the image push:

| Check | Result |
| --- | --- |
| Server | `v1.36.4-gke.1391002`, platform `linux/amd64` |
| Node | one amd64 node, kubelet `v1.36.4-gke.1391002`, allocatable 3920m CPU and 13591632Ki memory |
| Requests already on the node | 1435m CPU (36%), about 24% of memory |
| `inventory-service-v0-1-0` | ready 2/2, digest-pinned |
| `inventory-service-v0-2-0` | ready 1/1, `INVENTORY_FAILURE_PERCENT=0`, digest-pinned |
| `orders-service` | ready 2/2, digest-pinned |
| VirtualService `inventory-service` | `v0-1-0=100`, `v0-2-0=0` |
| DestinationRule subsets | `v0-1-0` is `app.kubernetes.io/version=v0.1.0`, `v0-2-0` is `v0.2.0` |
| PeerAuthentication `default` | STRICT |
| Namespace `platformforge-dev` | `istio-injection=enabled`, pod security `restricted` |
| Services | ClusterIP. No controller Service |
| `prometheus-server` | ClusterIP port 80, endpoint ready on port 9090. The live config has no `platformforge-canary` job |
| Prometheus RBAC | ClusterRole `platformforge-prometheus` allows get/list/watch on pods, services, endpoints, and endpointslices. The binding is a RoleBinding in `platformforge-dev`. No ClusterRoleBinding for that role |
| Controller objects | ServiceAccount, Deployment, and Service are absent |

## Published image

`us-central1-docker.pkg.dev/platformforge-dev-mn2026/platformforge/canary-controller@sha256:b7e8af0a35fd1d968ee3aacf701701e15da8ff600d6360662885f6c5846cc6a2`

`gcloud artifacts docker images describe` returned that digest. It is the same
id as the local `linux/amd64` image `platformforge/canary-controller:0.1.0-amd64`.
The image config user is `65532:65532`. Its environment has no token, key, or
password. A filename scan matched library modules whose names contain `token`
and the public CA bundle `certifi/cacert.pem`. The kind tag
`platformforge/canary-controller:0.1.0` is still `linux/arm64`
`sha256:068d3f8bac469f943af2d662320ff952712aa7afe7d83c92fb4378fcf0e9e7c9`
and was not loaded or changed. No second Artifact Registry repository was created.

## Measured rollback

Context `gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`.
Pod `canary-controller-68c45c499b-s7pgh`. Before the drill the route was
`v0-1-0=100`, `v0-2-0=0`, `INVENTORY_FAILURE_PERCENT=0`, and
`platformforge_canary_rollbacks_total` was 0. Inventory source counters were
`v0.1.0/200=437`, `v0.1.0/403=7`, `v0.2.0/200=20`, `v0.2.0/500=26`.

The faulty manifest was rendered with the locked inventory `0.2.0` digest and
applied. The canary became ready in 16.3s with failure percent 100. The
reviewed 90/10 VirtualService was applied. Both orders sidecars showed weights
90 and 10 after 1.62s. The controller logged `baseline recorded` at
`2026-10-21T06:40:01.925945017Z`.

Four hundred orders checks then returned HTTP 200 × 363 and HTTP 502 × 37 in
13.3s. At `2026-10-21T06:40:17.079311803Z`, 15.15s after the baseline and 1.77s
after the burst finished, the controller logged `action=rollback`,
`reason=canary error rate above threshold`, `error_ratio=1.0`,
`canary_total=16`, `canary_errors=16`, `route_action=rollback`. The p95
regression was 3.31 and was not the reason. The VirtualService read 100/0
1.04s after that log. Both orders sidecars showed only the `v0-1-0` cluster at
weight 100, 2.19s after the log. The next 20 orders checks were HTTP 200.
From the first request of the burst to the end of those 20 checks was 20.64s.

Prometheus, after the following scrape, showed `v0.2.0` HTTP 500 at 63, which
is 37 above the baseline and matches the 37 client 502s. The decision used the
16 errors scraped on that poll. `v0.1.0` HTTP 200 rose by 383, which is the
363 successes plus the 20 recovery requests. `platformforge_canary_rollbacks_total`
is 1, `platformforge_canary_decisions_total{action="rollback"}` is 1, and
`platformforge_canary_state{state="stable"}` is 5. After the route returned to
100/0 the error-ratio gauge is NaN.

The healthy canary manifest was applied again. `INVENTORY_FAILURE_PERCENT` is
0. Stable inventory stayed ready 2/2 with zero restarts. The restored canary
pod had one transient readiness response of HTTP 500 and an Istio startup
probe refusal, then became ready with zero restarts. Orders stayed ready 2/2.
PeerAuthentication and AuthorizationPolicy specs were unchanged. Every Service
is still ClusterIP. No PersistentVolume was created. The controller was not
deleted.

Grafana `/api/health` returned database ok, version 13.2.4. Datasource health
for Prometheus, Loki, and Tempo returned HTTP 200. The dashboard search lists
`PlatformForge services` (`platformforge-overview`). The browser UI was not
opened and the admin password was not recorded.

`pytest tests/gke -m gke` with `PLATFORMFORGE_GKE_CONTEXT` set: 4 passed in
14.36s. `make test`: 392 passed, 70 skipped, 0 failed in 12.61s.

Adding the controller asks for 50m CPU and 64Mi memory. The node has room.
That does not create a node, a disk, or a load balancer.

## Controller behavior once it is running

The process polls every 15 seconds. It does not apply the 90/10 file. The
operator applies that file for the recording. The first successful read of
90/10 stores a Prometheus counter baseline and holds. Later polls use the
increase since that baseline.

| Condition | Route write |
| --- | --- |
| Weights 100/0 | None. Episode resets |
| Error ratio above 0.05 | Reviewed stable file, 100/0 |
| p95 regression above 5 and the gap above 200 ms | Reviewed stable file |
| Missing telemetry or a Prometheus timeout through the 120 second window | Reviewed stable file |
| Healthy sample of at least 10 canary requests | Reviewed promote file, 0/100 |
| Same stable file applied again | None |

Stdout is one JSON object per decision. `GET /metrics` is the only HTTP path.
The pod is not injected, because Prometheus scrapes the pod IP and STRICT
would reject that path through a sidecar.

## Approval required before any live step

These steps were executed once on 2026-10-21. Repeating the failure drill
needs a new approval. The controller is already running.

1. Build `linux/amd64` from `deploy/kind/canary-controller.Dockerfile` and push
   only `canary-controller:0.1.0` to the existing repository
   `us-central1-docker.pkg.dev/platformforge-dev-mn2026/platformforge`.
   Do not create a repository. Record the digest.
2. Put that digest in `deploy/gke/canary-controller.yaml` and change the
   marker to `image-pushed: true`.
3. Apply only `deploy/gke/canary-controller.yaml` with
   `--context gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`.
4. Upgrade the existing Prometheus release on that context so the live config
   gains job `platformforge-canary` from `deploy/observability/prometheus-values.yaml`.
   Review a helm diff first. Persistence is disabled, so a server restart
   drops the in-memory TSDB. This is not a new GCP product.
5. Run the failure steps below. Restoring the healthy canary and the 100/0
   route is part of the same approval.

Do not apply `deploy/kind/canary-controller.yaml` to GKE. That file sets the
context env to `kind-platformforge`.

## Recording script

Show the current context first.

```bash
kubectl config current-context
```

Expect `gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`.

Confirm the baseline. These are reads.

```bash
CTX=gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev
kubectl --context "$CTX" -n platformforge-dev get deploy \
  inventory-service-v0-1-0 inventory-service-v0-2-0
kubectl --context "$CTX" -n platformforge-dev get virtualservice inventory-service \
  -o jsonpath='{range .spec.http[0].route[*]}{.destination.subset}={.weight}{"\n"}{end}'
```

Expect both Deployments ready and weights `v0-1-0=100`, `v0-2-0=0`.

After the image digest is in the manifest and the apply is approved:

```bash
kubectl --context "$CTX" apply -f deploy/gke/canary-controller.yaml
kubectl --context "$CTX" -n platformforge-dev rollout status deploy/canary-controller --timeout=180s
kubectl --context "$CTX" -n platformforge-dev logs deploy/canary-controller --tail=5
```

Expect Ready 1/1 and a log line `route is already stable`. A missing image,
a refused context, or a refused Prometheus URL must leave the pod unready.
That is fail closed.

### Failure and rollback

Deploy the faulty canary through the PlatformForge client, not `kubectl apply`
of a raw Deployment. The manifest is
`services/manifests/incidents/inventory-service-v0.2.0-faulty.yaml`.
`INVENTORY_FAILURE_PERCENT` becomes `100`. Readiness stays ready because
`/readyz` does not use that handler.

Apply `deploy/istio/platformforge-dev/inventory-virtualservice-canary.yaml`
with the pinned context. Wait until the orders sidecars show 90/10. Wait for
one controller log whose reason is `baseline recorded`. Then send traffic
through the mesh client to
`orders-service.platformforge-dev.svc.cluster.local:8080/orders/check/widget`.
Two hundred requests are enough for a 10 percent slice to pass 10 canary
samples.

Expect a JSON line with `action=rollback`, reason `canary error rate above
threshold`, and `route_action=rollback`. The next reads of the VirtualService
and the orders sidecars show `v0-1-0=100`. A further 20 requests return HTTP
200 while the canary is still faulty. That is the route restore, not a
rebuilt canary.

`inventory-service-v0-1-0` stays ready. The controller cannot delete it.

### Grafana

Port-forward is a local process, not a manifest change.

```bash
kubectl --context "$CTX" -n platformforge-observability port-forward \
  svc/prometheus-server 19091:80 --address 127.0.0.1
```

Query `platformforge_canary_rollbacks_total`, `platformforge_canary_state`,
`platformforge_canary_error_ratio`, `platformforge_canary_latency_regression`,
and `platformforge_canary_decisions_total`. After the rollback, the rollback
counter is at least 1 and the state gauge is 5 (`stable`) once the route
write has succeeded. Stop the port-forward.

The dashboard JSON is already in git. The GKE Grafana ConfigMap was not
updated in the kind controller work. Loading
`deploy/observability/dashboards/platformforge.json` into the GKE ConfigMap
`platformforge-dashboards` is a separate apply. Do it only with the same
approval, and do not read the Grafana admin password into the recording.

### Recovery

Redeploy `services/manifests/inventory-service-v0.2.0.yaml`. Confirm
`INVENTORY_FAILURE_PERCENT=0`. Confirm the VirtualService is still 100/0.
Confirm orders requests return HTTP 200.

## Cleanup

Approved only:

```bash
kubectl --context "$CTX" -n platformforge-dev delete \
  deploy/canary-controller svc/canary-controller \
  role/platformforge-canary-controller \
  rolebinding/platformforge-canary-controller \
  sa/platformforge-canary-controller
```

Do not delete `inventory-service-v0-1-0`, `inventory-service-v0-2-0`, or
`orders-service`. Do not delete the namespace. Leave STRICT and the inventory
AuthorizationPolicy as they are.

## Cost

No new cluster, node, disk, load balancer, or API. The pod fits on the
existing `e2-standard-4`. The image, once pushed, is bytes in the repository
that already exists. A Prometheus restart for the scrape job drops the
emptyDir TSDB and does not add a PersistentVolume. Egress for the recording
is the same private ClusterIP path the mesh already uses.

## Duration

After the image is in the registry: about 2 minutes to become Ready, about
5 minutes for the faulty rollout, the 90/10 shift, the rollback, and the
healthy restore, and about 5 minutes to port-forward Prometheus and read the
five series. Budget 30 minutes with the build and the helm diff included.
The five-experiment kind suite is not the GKE recording. It stays on kind.
