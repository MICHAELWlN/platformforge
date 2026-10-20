# Incident runbook: failing inventory-service canary

Covers a canary release of inventory-service that is "up but wrong": pods are Ready and
health checks pass, but a share of real requests fail. It also records the controlled drill
that was run on the local kind cluster (Milestone 3, Phase F) and its measured results.

Local kind cluster only. Prerequisites: [ISTIO.md](ISTIO.md), [CANARY_RELEASE.md](CANARY_RELEASE.md).

## Symptoms

- orders-service returns `502 {"detail": {"error": "inventory_bad_status", ...}}` for a
  fraction of `GET /orders/check/{item}` calls, roughly equal to the canary weight.
- Kubernetes shows nothing wrong: all inventory pods are `Running` / `Ready` with 0
  restarts, and `platformforge status` reports `HEALTHY` (health endpoints are unaffected).
- inventory v0.2.0 logs `"injected failure"` (drill) or other errors (real incident).

## Diagnose (read-only)

```bash
export CTX=kind-platformforge NS=platformforge-dev

# 1. Which version is failing? Per-version response codes from orders' sidecars:
for p in $(kubectl --context $CTX -n $NS get pod -l app.kubernetes.io/name=orders-service -o name); do
  kubectl --context $CTX -n $NS exec $p -c istio-proxy -- pilot-agent request GET stats \
    | grep '^istiocustom.istio_requests_total.reporter.source' | grep 'destination_service.inventory-service' \
    | grep -oE 'destination_version\.v[0-9.]+|response_code\.[0-9]+|: [0-9]+$' | paste - - -
done
#   destination_version.v0.2.0  response_code.500  : 42    <- canary failing
#   destination_version.v0.1.0  response_code.200  : 458   <- stable healthy

# 2. Current routing
kubectl --context $CTX -n $NS get virtualservice inventory-service \
  -o jsonpath='{range .spec.http[0].route[*]}{.destination.subset}={.weight}{"\n"}{end}'

# 3. Follow one failing request end to end by its X-Request-ID
kubectl --context $CTX -n $NS logs deploy/orders-service -c istio-proxy --since=5m | grep '"response_code":500'
kubectl --context $CTX -n $NS logs deploy/inventory-service-v0-2-0 -c inventory-service --since=5m | grep -i error
```

## Remediate

Shift all traffic to the healthy version first. This is fast, does not touch any pods, and
is reversible:

```bash
kubectl --context $CTX apply -f deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml
```

Verify that every orders sidecar now uses only the stable subset (expect a single cluster
`outbound|8080|v0-1-0|inventory-service...`):

```bash
istioctl proxy-config route deploy/orders-service -n $NS --name 8080 -o json | grep -o 'outbound|8080|v0-[0-9-]*|inventory[^"]*'
```

Then confirm recovery: orders-service returns 200s again, and the per-version stats show
v0.2.0 counts no longer increasing.

## Restore a stable configuration

Leave the stable VirtualService in place. Fix the canary (redeploy a known-good
configuration) and verify it in isolation before routing any traffic to it again:

```bash
platformforge deploy services/manifests/inventory-service-v0.2.0.yaml --approve-context $CTX
platformforge status services/manifests/inventory-service-v0.2.0.yaml --approve-context $CTX
```

Re-enter the canary process ([CANARY_RELEASE.md](CANARY_RELEASE.md)) only after the root
cause is understood.

## What not to do

- Do not delete the canary Deployment or scale v0.1.0 as a first response. Routing is the
  fast, reversible lever, and deleting removes the evidence.
- Do not loosen the AuthorizationPolicy or set mTLS to PERMISSIVE to "rule things out".
  The failure is visible at the HTTP status layer, not the security layer.
- Do not judge canary health by pod readiness alone. Readiness only checks `/readyz`.

## Drill record (Milestone 3, Phase F)

Controlled and reversible. Only the canary's configuration was changed; nothing was
deleted. Automated as `tests/integration/test_services_mesh.py::test_f1`–`test_f3`.

| Step | Action | Observed |
| --- | --- | --- |
| Start | 90/10 canary routing, v0.2.0 healthy | 1000/1000 requests 200 |
| Fault | `platformforge deploy services/manifests/incidents/inventory-service-v0.2.0-faulty.yaml` (same image, `INVENTORY_FAILURE_PERCENT=100`) | canary pod rolled, Ready, `HEALTHY` |
| Detect | 500 requests through orders-service | runs 1–4: **42, 49, 46, 44 of 500** (8.4%, 9.8%, 9.2%, 8.8%) returned 502. Every failure matched a v0.2.0 HTTP 500 in Istio's metrics; v0.2.0 served 0 successes; v0.1.0 served only 200s; canary log shows `injected failure` |
| Remediate | apply `inventory-virtualservice-stable.yaml` | both orders sidecars converged on 100% v0-1-0 in **1.1 s** (all four runs) |
| Verify | 300 requests through orders-service | **300/300 200**, all to v0.1.0, 0 to v0.2.0 |
| Restore | redeploy healthy `inventory-service-v0.2.0.yaml`; keep stable routing | canary env `INVENTORY_FAILURE_PERCENT=0`; in-pod lookup 200; VirtualService 100/0; 50/50 requests 200 |

Expected failure count: each request reaches v0.2.0 with p = 0.1, so failures in 500
requests are binomial with mean 50 and sd ≈ 6.7. All four runs fall within ±1.2 sd. The
test accepts 20–85.

Impact window in the drill: from the faulty rollout until the stable VirtualService
converged. About 10% of requests failed during that window. Time to detect depended on
when the operator looked; there is no alerting yet (observability is a later milestone).

Root cause in the drill: a configuration change (`INVENTORY_FAILURE_PERCENT=100`) in the
canary manifest. The health endpoints were unaffected, so Kubernetes readiness could not
catch it. Only request-level signals (status codes per version) revealed it.

Follow-ups (not implemented): alerting on per-version error rate, automated canary analysis
and rollback, and an outlier-detection/retry policy reviewed for production.
