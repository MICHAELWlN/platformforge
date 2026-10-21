# Rollback runbook

Use this when a candidate inventory digest is receiving traffic, or when a
delivery times out before traffic starts. The known-good route is
`deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml`
(`v0-1-0=100`, `v0-2-0=0`). The known-good canary manifest is
`services/manifests/inventory-service-v0.2.0.yaml` (`INVENTORY_FAILURE_PERCENT=0`).

Context must be
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`.

## When to roll back

`rollback_plan` emits steps for:

| Condition | Plan |
| --- | --- |
| Weights are not 100/0 | Re-apply the stable VirtualService |
| Canary image is not the previous digest | `kubectl set image` back to that digest |
| `INVENTORY_FAILURE_PERCENT` is not 0 | Re-apply the healthy v0.2.0 manifest |

`decide` selects rollback for elevated HTTP 5xx, a success rate under the
floor, a canary that is not Ready, a deployment timeout, a failed
authorization check, missing telemetry, and an observation window that ended
with fewer requests than `min_requests`.

A second call on an already-stable `ReleaseState` returns an empty plan. That
is the idempotent case. None of the commands delete a Deployment, Service, or
pod. v0.1.0 keeps serving during the rollback.

## Operator steps

1. Show the current context and confirm it is the pinned GKE context.
2. Apply the stable VirtualService first so new requests leave the candidate.
3. If the canary Deployment is on a bad digest or `INVENTORY_FAILURE_PERCENT`
   is not 0, re-apply the healthy manifest or set the previous digest.
4. Wait for `inventory-service-v0-2-0` to be Ready. Probes hit the sidecar and
   keep passing when only the lookup path is failing, so also send
   `GET /orders/check/widget` and expect HTTP 200.
5. Leave the stable VirtualService in place. Do not delete the canary
   Deployment; weight 0 is the off switch.
6. Keep pod logs. Rollback does not restart the telemetry stack.

`python -m platformforge.delivery refuse-apply` is the automated path's stop.
Running the commands above is a manual approval, same as the incident drill
in [GKE_INCIDENT_DEMO.md](GKE_INCIDENT_DEMO.md).

## Evidence to keep

- The Prometheus query for `istio_requests_total` by `response_code`.
- One inventory log line with `request_id` and `trace_id`.
- The Tempo trace for that id, if Tempo still has it. Disks are emptyDir, so
  a pod restart drops this evidence.
- The VirtualService weights after recovery.

## Do not

- Delete the v0.1.0 Deployment to "finish" a rollback.
- Point `platformforge deploy` at the GKE context. The CLI refuses it.
- Weaken PeerAuthentication or the inventory AuthorizationPolicy so a probe
  or a client can bypass the mesh.
