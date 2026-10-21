# Kind canary controller

The controller is a single-replica Deployment in `platformforge-dev` on
`kind-platformforge`. It polls the inventory VirtualService, compares stable
`v0.1.0` with canary `v0.2.0`, and applies one reviewed route file. It does
not create the 90/10 split. The experiment harness does that.

Do not apply `deploy/kind/canary-controller.yaml` to GKE. The image
`platformforge/canary-controller:0.1.0` is local. It was not pushed.

## Loop

One decision per poll. The kind Deployment uses a 15 second poll, a 120 second
observation window, and a 180 second maximum episode. The sleep between polls
is sliced into one-second waits so SIGTERM is noticed. There is no inner retry.
The tick cap is `floor(max_episode / poll) + 1`. A hold that is still open at
the maximum episode becomes a rollback.

| Observed weights | Action |
| --- | --- |
| `100/0` | Hold. Reset the episode and the counter baseline. Do not write. |
| `90/10` or `0/100`, first successful read | Hold. Store the counter baseline. Do not write. |
| `90/10` and the delta says promote | Apply `inventory-virtualservice-promote.yaml` (`0/100`). |
| `0/100` and the delta still says promote | Hold. The promoted route stays. Do not write again. |
| Rollback | Apply `inventory-virtualservice-stable.yaml` (`100/0`). |
| Any other weights | Roll back immediately. |
| Route unreadable | Hold until the maximum episode, then apply stable. |
| Apply raises `CanaryError` or `OSError` | Record `failed`. The next poll tries once. |
| SIGTERM or SIGINT while the last weights were not `100/0` | Apply stable once, then exit. |

Each line on stdout is one JSON object with `event=decision` or `route_failed`.
Fields include the action, reason, error ratio, p95 regression, both p95
values, canary total, canary errors, route action, and episode age.

The comparison uses the counter increase since the baseline, not a PromQL
`increase()` range. A burst that finished before the baseline is not part of
the episode. The harness waits for the log reason `baseline recorded` before
it sends the measured traffic. A counter that moves backwards refreshes the
baseline and holds.

Decision order is the analyzer in
[AUTOMATED_CANARY.md](AUTOMATED_CANARY.md). On this Deployment the p95 rollback
also requires the canary p95 to be more than 200 ms above the stable p95.
A large ratio on a sub-millisecond baseline does not roll back.

## Metrics

`GET /metrics` is the only served path. Other paths return 404. The pod listens
on `0.0.0.0:8080` because the Service is ClusterIP and the manifest sets no
`hostPort`. Readiness and liveness use that path, so a Prometheus outage does
not kill the pod.

Prometheus job `platformforge-canary` in
`deploy/observability/prometheus-values.yaml` keeps pods in
`platformforge-dev` with label `app.kubernetes.io/name=canary-controller` and
port name `metrics`. The existing `istio-proxy` job was not widened. The
controller sets `sidecar.istio.io/inject: false` so the scrape is plaintext
pod-IP traffic. Injected workloads stay STRICT.

Series:

- `platformforge_canary_rollbacks_total`
- `platformforge_canary_state` (0 baseline, 1 canary_10, 2 analyzing, 3 promoting, 4 rolling_back, 5 stable, 6 failed)
- `platformforge_canary_error_ratio`
- `platformforge_canary_latency_regression`
- `platformforge_canary_decisions_total{action="promote|hold|rollback"}`

A restart resets the counters. The missing-telemetry experiment replaces the
pod, so the fail-closed rollback is a new series.

## Grafana

`deploy/observability/dashboards/platformforge.json` adds the controller
panels to the Istio canary panels. The kind ConfigMap
`platformforge-dashboards` was read back and lists:

- Canary versus stable error rate
- Canary versus stable p95 latency
- Inventory traffic distribution
- Canary rollbacks
- Canary rollout state
- Controller canary error ratio
- Controller latency regression
- Canary decision history

Decision history is the decisions counter by action. The JSON log is the
per-decision record. The Grafana UI was not opened and the admin password was
not read. The GKE dashboard ConfigMap was not changed.

## Experiment workflow

Current context stays the pinned GKE context. Every cluster call passes
`--context kind-platformforge` or `persist_config=False`.

```bash
docker build --platform linux/arm64 --provenance=false \
  -f deploy/kind/canary-controller.Dockerfile \
  -t platformforge/canary-controller:0.1.0 .
kind load docker-image platformforge/canary-controller:0.1.0 --name platformforge
kubectl --context kind-platformforge -n platformforge-dev apply -f deploy/kind/canary-controller.yaml
make canary-controller-kind
```

`make test` does not set `PLATFORMFORGE_CANARY_IT`, so this file is skipped.
`make canary-kind` is the older one-shot drill, not these five experiments.
The test restores the stable VirtualService, the healthy canary manifest, and
the in-cluster Prometheus URL before it returns.

## Measured run

Date: 2026-10-21. `PLATFORMFORGE_CANARY_IT=1 pytest tests/integration/test_canary_controller_kind.py`
passed in 335.84s. Current context was
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev` at the start
and at the end.

| Experiment | Decision | Measurement |
| --- | --- | --- |
| 1. Healthy 90/10, 400 requests | promote, `error rate and p95 are inside the thresholds` | HTTP 200 × 400. Canary total 56, error ratio 0. Stable p95 0.4777777777777778 ms, canary p95 0.4925925925925925 ms, regression 0.03100775193798431. Episode age 15.01 s. Both orders sidecars then showed only `v0-2-0` at weight 100. |
| 2. Faulty canary, `INVENTORY_FAILURE_PERCENT=100`, 400 requests | rollback, `canary error rate above threshold` | HTTP 200 × 348, HTTP 502 × 52. Canary total 52, canary errors 52, error ratio 1.0. After the decision log, sidecar weight 100 was already present in 0.071 s. The next 20 requests were HTTP 200. |
| 3. `INVENTORY_DELAY_MS=1000`, failure percent 0, 220 requests | rollback, `canary p95 latency regressed` | HTTP 200 × 220. Error ratio 0. Canary total 15. Stable p95 0.95 ms, canary p95 975 ms, regression 1025.32. The next 20 requests were HTTP 200. |
| 4. Prometheus URL `http://127.0.0.1:9` | no promote; route returned to 100/0 | Fail-closed interval 129.4 s from the 90/10 apply until the sidecars showed 100/0. The URL was restored to `http://prometheus-server.platformforge-observability.svc.cluster.local`. |
| 5. Stable file applied twice | no route write | One later log was `route is already stable` with `route_action` null. `inventory-service-v0-1-0` ready replicas 2. SubjectAccessReview: delete `deployments` denied, update `virtualservices` allowed. |

Prometheus instant queries around timestamp `1792563679`, summed across pods:

| Offset | `rollbacks_total` | `state` | `error_ratio` | `latency_regression` | decisions |
| --- | --- | --- | --- | --- | --- |
| -270s | 2 | 5 stable | 0 | 0.03100775193798431 | promote 2, rollback 2 |
| -210s | 3 | 5 stable | NaN | NaN | rollback 3 |
| -180s | 4 | 5 stable | 0 | 1025.3157894736842 | rollback 4 |
| -150s | 0 | 2 analyzing | NaN | NaN | counters reset on the URL change |
| -30s | 1 | 5 stable | NaN | NaN | rollback 1 |

The value 2 at -270s includes rollbacks from earlier attempts on that same
pod, before this passing run. The passing run then added one promote, the
faulty rollback, the latency rollback, and, after the pod replacement, the
fail-closed rollback.

After the test restored the Prometheus URL, the new pod
`canary-controller-7b9479b95b-6rjst` scraped as job `platformforge-canary`:
rollbacks 0, state 5, error ratio NaN, latency regression NaN, hold decisions 4.
A 5 minute Istio query at that moment still showed inventory `v0.2.0` p95 of
975 ms and a non-zero `v0.1.0` rate. The route left behind was `100/0`, and
`INVENTORY_FAILURE_PERCENT` was `0`.

## Security

The ServiceAccount is `platformforge-canary-controller` in `platformforge-dev`.
The Role can `get` Deployments `inventory-service-v0-1-0` and
`inventory-service-v0-2-0`, and `get` plus `update`
`virtualservices/inventory-service`. It cannot delete, list, exec, or read
Secrets. It has no access outside the namespace. It is not cluster-admin.

The process refuses to start unless `PLATFORMFORGE_CANARY_CONTEXT` is
`kind-platformforge` and `PLATFORMFORGE_CANARY_IN_CLUSTER` is `1`. That check
is the environment variable. It is not the API server's cluster identity. A
copy of this manifest on another cluster, with the same variable, would use
whatever API that pod can reach. Do not copy it.

STRICT mTLS, the inventory AuthorizationPolicy, and the private ClusterIP
Services were not changed. The controller is not injected. The metrics Service
has no external IP, NodePort, or host port.

## GKE

Deployed on the pinned context from `deploy/gke/canary-controller.yaml`.
The image is `canary-controller@sha256:b7e8af0a35fd1d968ee3aacf701701e15da8ff600d6360662885f6c5846cc6a2`.
The measured rollback is [GKE_CANARY_EXPERIMENT.md](GKE_CANARY_EXPERIMENT.md).
Terraform and GitHub Actions were not changed.

| Check | Result |
| --- | --- |
| Cluster compatibility | The pinned cluster is Kubernetes v1.36.4. The controller uses the same Kubernetes Python client as the rest of the tree. The kind manifest is still refused by the context check. |
| Resources | One replica, requests 50m CPU and 64Mi memory, limits 200m and 128Mi. `emptyDir` `/tmp` of 64Mi. No PersistentVolume, load balancer, or public IP. |
| RBAC | The same Role is bound in `platformforge-dev` on GKE. `kubectl auth can-i` allowed get on the two inventory Deployments and get/update on VirtualService `inventory-service`. Update of orders, delete, and Secrets were denied. |
| Prometheus | Job `platformforge-canary` is in the live GKE server config. `up` for that job was 1. The server pod was not replaced. |
| Cost | One extra small pod on the existing node. No new GCP API and no Terraform change. |
| Rollback safety | The controller can replace one VirtualService from the reviewed stable or promote file. It cannot delete `inventory-service-v0-1-0`. |

## Limits

- The 90/10 file is applied by the harness, not by the controller.
- Traffic sent before `baseline recorded` is excluded from the decision.
- Counters reset when the pod is replaced. Experiment 4 does that on purpose.
- Grafana shows counters, not the JSON decision log.
- The absolute 200 ms p95 floor is a lab control. A few milliseconds of ratio
  noise does not roll back. A 1000 ms delay does.
- `make integration` does not select this test. It has no `integration` marker.
