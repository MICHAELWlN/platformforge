# Canary release: inventory-service v0.1.0 → v0.2.0

How a new inventory-service version is introduced alongside the stable one and receives a
controlled share of traffic through Istio. Local kind cluster only. Prerequisites:
[ISTIO.md](ISTIO.md).

## Model

```
                    orders-service pod
  orders app ──HTTP──▶ orders sidecar (Envoy)
                         │  VirtualService inventory-service: weights per request
                         │  DestinationRule subsets by label app.kubernetes.io/version
                ┌────────┴────────────────┐ mTLS (ISTIO_MUTUAL, auto)
                ▼ 90%                      ▼ 10%
   subset v0-1-0                       subset v0-2-0
   Deployment inventory-service-v0-1-0 Deployment inventory-service-v0-2-0
   2 pods, version=v0.1.0              1 pod, version=v0.2.0
                └──── one Service: inventory-service (selector: name + managed-by) ────┘
```

- PlatformForge renders one Deployment per `spec.version`, named `<name>-<version with
  dots as dashes>`, whose selector and pod labels include `app.kubernetes.io/version`. The
  Service selector omits the version, so one Service and one DNS name cover every track.
- Both tracks run as the same workload identity (`serviceAccountName: inventory-service`),
  so the inventory AuthorizationPolicy and every caller see the same principal.
- The split is applied by the **caller's** sidecar on every request. It does not depend on
  pod counts (2 stable pods vs 1 canary pod would give ~33% under plain kube-proxy
  balancing).
- orders-service has a single, unversioned `INVENTORY_URL`. It has no knowledge of versions
  and does no random selection.

## Files

| File | Role |
| --- | --- |
| `services/manifests/inventory-service.yaml` | stable track `v0.1.0`, image `0.1.0`, 2 replicas |
| `services/manifests/inventory-service-v0.2.0.yaml` | canary track `v0.2.0`, image `0.2.0`, 1 replica |
| `deploy/istio/platformforge-dev/inventory-destinationrule.yaml` | subsets `v0-1-0`, `v0-2-0` |
| `deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml` | 100 / 0 |
| `deploy/istio/platformforge-dev/inventory-virtualservice-canary.yaml` | 90 / 10 |

What v0.2.0 changes: the image version (`/version` reports `0.2.0`) and an opt-in,
bounded fault knob `INVENTORY_FAILURE_PERCENT` (0–100, default 0) used for failure drills.
Lookups and the response schema are unchanged.

## Procedure

```bash
export CTX=kind-platformforge
make images kind-load                                   # builds/loads *:0.2.0
kubectl --context $CTX apply -f deploy/istio/platformforge-dev/inventory-destinationrule.yaml
kubectl --context $CTX apply -f deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml

# 1. Deploy the canary. It becomes a Ready endpoint of the Service but gets 0% traffic.
platformforge deploy services/manifests/inventory-service-v0.2.0.yaml --approve-context $CTX

# 2. Shift 10% to the canary
kubectl --context $CTX apply -f deploy/istio/platformforge-dev/inventory-virtualservice-canary.yaml

# 3. Watch it (per-version success/error counts from orders' sidecars)
for p in $(kubectl --context $CTX -n platformforge-dev get pod -l app.kubernetes.io/name=orders-service -o name); do
  kubectl --context $CTX -n platformforge-dev exec $p -c istio-proxy -- pilot-agent request GET stats \
    | grep '^istiocustom.istio_requests_total.reporter.source' | grep 'destination_service.inventory-service' \
    | grep -oE 'destination_version\.v[0-9.]+|response_code\.[0-9]+|: [0-9]+$' | paste - - -
done

# 4a. Roll back (any time): 100% to v0.1.0
kubectl --context $CTX apply -f deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml
```

To confirm that the sidecars picked up a change, read the weights Envoy is actually using:
`istioctl proxy-config route deploy/orders-service -n platformforge-dev --name 8080 -o json`
(the integration tests do the same through the Envoy admin API, `tests/integration/mesh.py::route_weights`).

Promotion to 100% v0.2.0 (and retiring v0.1.0) is not part of this milestone.

## Measured distribution

Each sample sends `N = 1000` sequential requests from the in-mesh test client to
`orders-service`, which calls inventory once per request. Counts come from the
`istio_requests_total` deltas reported by the orders sidecars (`reporter=source`), labeled by
`destination_version`. The canary weights were confirmed in each orders sidecar's live Envoy
route configuration before sampling. All 1000 requests returned 200 in every sample.

| Sample | v0.1.0 | v0.2.0 | v0.2.0 share | Deviation from 100 |
| --- | --- | --- | --- | --- |
| Run 1 (integration) | 902 | 98 | 9.8% | −0.2 σ |
| Run 2 (full suite) | 887 | 113 | 11.3% | +1.4 σ |
| Run 3 (integration) | 919 | 81 | 8.1% | −2.0 σ |
| Run 4 (final full suite) | 912 | 88 | 8.8% | −1.3 σ |
| **Total (4 × 1000)** | **3620** | **380** | **9.5%** | −1.1 σ (pooled sd ≈ 19.0) |

Control (same deployment, stable VirtualService 100/0): 200 of 200 requests went to v0.1.0
and **0** to v0.2.0, although the v0.2.0 pod was a Ready endpoint of the Service (3 ready
endpoints: 2 × v0.1.0, 1 × v0.2.0).

### Expected statistical variation

Each request independently goes to v0.2.0 with probability p = 0.10, so the canary count is
binomial: mean N·p = 100, standard deviation √(N·p·(1−p)) = √90 ≈ 9.49.

- About 68% of samples should fall in 91–109, about 95% in 81–119, and about 99.7% in 72–128.
- The test accepts 60–140 (about ±4.2 σ; a false failure is roughly 1 in 35,000 runs).
- Pod-proportional balancing would give about 333 ± 15. That is more than 20 σ away from
  the observed values, so the observed ~10% split comes from Istio's weights, not from the
  pod ratio.
- Smaller samples are noisy. At N = 100 the canary count is 10 ± 3, so "7%" or "13%" is
  normal and not evidence of misrouting.

### Why this proves routing is done by Istio

1. Pod ratio is 2:1, yet the share matches the configured 90/10 weights, not 67/33.
2. With the stable VirtualService the Ready canary receives exactly 0 requests.
3. The numbers are reported by Envoy in the orders sidecar per subset
   (`destination_version`), and Envoy's live route config shows the weighted clusters
   `outbound|8080|v0-1-0|…` = 90, `outbound|8080|v0-2-0|…` = 10.
4. orders-service has one upstream URL and no version-aware code.

## Limitations

- Per-request weighting only. There is no header- or user-based routing, no session
  affinity, and no automated analysis or promotion (e.g. Argo Rollouts/Flagger). Promotion
  is manual.
- Retries use Istio's defaults. In the failure drill, HTTP 500s were not retried: each
  v0.2.0 500 surfaced as exactly one client error. A failing canary is therefore fully
  visible to callers. This is intended for the drill, but it is not a tuned production
  policy.
- Weight changes reach the sidecars in about 1 s on this single-node cluster; this was not
  measured under load or across nodes.
