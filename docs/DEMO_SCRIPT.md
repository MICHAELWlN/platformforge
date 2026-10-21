# Four-minute spoken script

Read this against [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md). Speak only numbers that
the screen shows in this take, or name the 2026-10-21 measurement as a past run.

## 0:00 GKE

“This is one private zonal cluster, `platformforge-dev`, Kubernetes 1.36.4.
One node, no public Service. Terraform created it. This recording does not
apply Terraform again.”

Show the node and the 100/0 route.

## 0:25 GitHub Actions

“Images publish through GitHub Actions OIDC to the existing Artifact Registry
repository. The workflow can push a digest. It still refuses to apply
manifests. What is running is digest-pinned.”

Point at a past successful run. Do not start one.

## 0:45 Istio

“Inventory is STRICT mTLS. Only the orders ServiceAccount may GET `/inventory/*`.
Stable `v0.1.0` has 100 percent. Canary `v0.2.0` has zero. The canary process
is ready, and its failure percent is zero.”

## 1:10 Failure

“I am shifting 10 percent to the canary and setting every inventory lookup to
HTTP 500. Readiness stays green, so Kubernetes does not catch it. Orders turns
that 500 into a client 502.”

If this take does not inject the failure, say: “The failure drill already ran.
I will not inject another one in this take.”

## 1:50 Grafana

“Prometheus is scraping the controller on a private ClusterIP. These panels
are the error rate, the traffic split, the rollback counter, and the rollout
state. They read the same series the controller uses.”

## 2:20 Rollback

“The controller does not choose the 90/10 route. After it records a baseline,
one poll compares the canary with stable. On 2026-10-21 that decision was
15.15 seconds after the baseline, the sidecars showed 100 percent stable 2.19
seconds later, and the client was healthy again 20.64 seconds after the first
failing request. If this take is a new drill, I will read the log timestamp
instead of repeating those numbers.”

## 2:45 Assistant

Ask, and wait for tool calls:

“Investigate the latest PlatformForge inventory-service canary failure.
Determine which version experienced errors, why the controller rolled back,
whether stable remained available, and what evidence supports your conclusions.
Distinguish verified facts from hypotheses.”

The answer has to say `verified_root_cause` is null. It may cite a `v0.2.0`
500 series, a controller line with `error_ratio` 1.0, and stable replicas 2/2
only if those came back from a tool. It must say VirtualService weights are
not in the tool list. It must not invent a trace, an event, or a recovery time.

## 3:35 Recovery

“The stable Deployment was never deleted. Restoring the healthy canary puts
the failure percent back to zero and leaves the route at 100/0.”

Show preflight. Stop.
