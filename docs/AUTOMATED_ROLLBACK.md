# Automated rollback

Rollback is a route change plus a record of the samples that caused it.
The stable Deployment stays in place.

## States

```
BASELINE -> CANARY_10 -> ANALYZING -> PROMOTING -> STABLE
                              |            \
                              v             -> FAILED when the restored route does not verify
                        ROLLING_BACK -> STABLE
```

`HOLD` stays in `ANALYZING`. A rollback event from `STABLE` stays `STABLE`.
An unknown event raises `CanaryError`. The transition table is
`platformforge.canary.transition`.

## Kind route change

`platformforge.canary_apply` checks the context and namespace before it reads
the API. It replaces `virtualservices/inventory-service` in `platformforge-dev`
from one of these reviewed files:

| Weights | File |
| --- | --- |
| `v0-1-0=100`, `v0-2-0=0` | `deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml` |
| `v0-1-0=90`, `v0-2-0=10` | `deploy/istio/platformforge-dev/inventory-virtualservice-canary.yaml` |
| `v0-1-0=0`, `v0-2-0=100` | `deploy/istio/platformforge-dev/inventory-virtualservice-promote.yaml` |

Any other weight, file, context, or namespace is refused. The replace keeps
the existing `resourceVersion`. The call path has no delete.

After the stable file is applied, the drill waits until the orders sidecars
report only `outbound|8080|v0-1-0|inventory-service.platformforge-dev.svc.cluster.local`
at weight 100. Istio omits the zero-weight canary cluster. A second apply of
the same file is the idempotent retry.

## Recovery checks

1. The decision and the Prometheus samples are kept on the `Analysis` object.
2. The stable VirtualService is applied.
3. Sidecar weights show the stable subset at 100.
4. `inventory-service-v0-1-0` is still present.
5. New orders requests succeed while the canary process is still on the faulty
   manifest. Success at that moment means the route is no longer using the canary.
6. The healthy canary manifest is deployed and `INVENTORY_FAILURE_PERCENT` is `0`.

The faulty manifest is `services/manifests/incidents/inventory-service-v0.2.0-faulty.yaml`.
The healthy manifest is `services/manifests/inventory-service-v0.2.0.yaml`.
Neither file deletes `inventory-service-v0-1-0`.

## GKE and CI

The one-shot kind drill still refuses the pinned GKE context. The controller
accepts that context only inside the pod, with `PLATFORMFORGE_CANARY_GKE=1`
and the service-account token. On 2026-10-21 it rolled the inventory route
from 90/10 back to 100/0 after a faulty canary. The samples are in
[docs/GKE_CANARY_EXPERIMENT.md](GKE_CANARY_EXPERIMENT.md). The delivery
workflow still exits 2 on its apply step. Publishing an image does not by
itself change a digest-pinned Deployment or a VirtualService.

The kind controller uses the same two writes: stable for rollback, promote for
a passing 90/10. After either write succeeds it records state `stable`, so a
later poll on `0/100` does not apply the promote file again. A failed write
records `failed` and the next poll tries once. Shutdown applies the stable
file once when the last observed weights were not `100/0`. See
[docs/CANARY_CONTROLLER.md](CANARY_CONTROLLER.md).
