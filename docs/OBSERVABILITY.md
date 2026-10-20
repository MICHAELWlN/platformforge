# Observability (Milestone 4)

Local-kind telemetry for `platformforge-dev`: Prometheus, Grafana, Loki, Tempo, and
Grafana Alloy, plus OpenTelemetry in the two FastAPI services and Istio's OTLP tracer.
Nothing here is exposed outside the cluster, and none of it has been run on GKE.

## Version selection

Checked 2026-10-20 against the chart repositories. Helm is 3.22.1 (darwin-arm64,
SHA-256 `e73da8a0b0d5193774fdff17ce6319c7436bb22ee229b45fd64f3c40368c753a`), kept at
Helm 3 because these charts document Helm 3. The binary lives in gitignored `.tools/helm`.

| Component | Chart | App | Chart repo |
| --- | --- | --- | --- |
| Prometheus | prometheus-community/prometheus 29.37.0 | v3.15.0 | prometheus-community.github.io/helm-charts |
| Grafana | grafana-community/grafana 14.0.1 | 13.2.4 (distroless) | grafana-community.github.io/helm-charts |
| Loki | grafana-community/loki 18.17.1 | 3.7.8 | grafana-community.github.io/helm-charts |
| Tempo | grafana-community/tempo 3.2.0 | 3.1.0 | grafana-community.github.io/helm-charts |
| Alloy | grafana/alloy 1.14.0 | v1.21.0 | grafana.github.io/helm-charts |

Kubernetes v1.37.0 and Istio 1.31.2 were already on the cluster. The Grafana, Loki, and
Tempo charts moved to `grafana-community` in 2026; that is the maintained source. Tempo
is the single-binary chart, not `tempo-distributed`. OpenTelemetry Python is
`opentelemetry-sdk` 1.45.1 and `opentelemetry-instrumentation-fastapi` / `httpx` 0.66b1.
Export is OTLP/HTTP so the distroless image does not need `grpcio`.

There is no OpenTelemetry Collector. Tempo 3.1 accepts OTLP on gRPC 4317 and HTTP 4318.
Alloy pushes logs with the Loki API. A collector would only add another process.

## Capacity

Measured on the single kind node before install: Docker Desktop memory 7.748 GiB with
about 1.9 GiB in use, node allocatable memory 8124256Ki, allocated requests 1630m CPU /
1570Mi memory. After the stack was up, allocated requests were 1860m CPU (15%) and
2562Mi memory (32%); limits were 9350m CPU (77%) and 6420Mi memory (80%). `docker stats`
showed the kind node at 3.792 GiB / 7.748 GiB. No Docker memory increase was required.

Each piece is one replica. Requests are Prometheus 50m/256Mi, Grafana 50m/128Mi,
Loki 50m/256Mi, Tempo 50m/192Mi, Alloy 20m/128Mi, plus a small config-reloader sidecar
on Prometheus and Alloy.

## Install

Context must be `kind-platformforge`. Set Helm's home under `.tools/` so it does not
write into the user cache:

```bash
export HELM_CACHE_HOME="$PWD/.tools/helm-cache"
export HELM_CONFIG_HOME="$PWD/.tools/helm-config"
export HELM_DATA_HOME="$PWD/.tools/helm-data"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo add grafana-community https://grafana-community.github.io/helm-charts
helm repo update

kubectl --context kind-platformforge apply -f deploy/observability/namespace.yaml
kubectl --context kind-platformforge apply -f deploy/observability/prometheus-rbac.yaml
kubectl --context kind-platformforge create configmap platformforge-dashboards \
  -n platformforge-observability \
  --from-file=platformforge.json=deploy/observability/dashboards/platformforge.json \
  --dry-run=client -o yaml | kubectl --context kind-platformforge apply -f -

NS=platformforge-observability
helm upgrade --install loki grafana-community/loki --version 18.17.1 -n $NS \
  -f deploy/observability/loki-values.yaml --wait --timeout 5m
helm upgrade --install tempo grafana-community/tempo --version 3.2.0 -n $NS \
  -f deploy/observability/tempo-values.yaml --wait --timeout 5m
kubectl --context kind-platformforge apply -f deploy/observability/tempo-networkpolicy.yaml
helm upgrade --install prometheus prometheus-community/prometheus --version 29.37.0 -n $NS \
  -f deploy/observability/prometheus-values.yaml --wait --timeout 5m
helm upgrade --install alloy grafana/alloy --version 1.14.0 -n $NS \
  -f deploy/observability/alloy-values.yaml --wait --timeout 5m
helm upgrade --install grafana grafana-community/grafana --version 14.0.1 -n $NS \
  -f deploy/observability/grafana-values.yaml --wait --timeout 5m
```

Then install the Istio tracing config (same Istio 1.31.2 revision) and rebuild the
service images so the running pods export traces. Image tags stay `0.1.0` and `0.2.0`;
`imagePullPolicy` is `IfNotPresent`, so the node must be loaded and the Deployments
rolled:

```bash
.tools/istio-1.31.2/bin/istioctl install -y -f deploy/istio/istio-operator.yaml \
  --context kind-platformforge
kubectl --context kind-platformforge apply -f deploy/istio/platformforge-dev/telemetry-tracing.yaml
make images kind-load
make images kind-load VERSION=0.1.0
.venv/bin/platformforge deploy services/manifests/inventory-service.yaml \
  --approve-context kind-platformforge
.venv/bin/platformforge deploy services/manifests/inventory-service-v0.2.0.yaml \
  --approve-context kind-platformforge
.venv/bin/platformforge deploy services/manifests/orders-service.yaml \
  --approve-context kind-platformforge
```

## What was configured

- Namespace `platformforge-observability`: `istio-injection=disabled`, Pod Security
  `baseline`. Sidecars are not injected here. Prometheus scrapes pod IPs on port 15020,
  and Tempo and Loki speak plaintext. A sidecar would send that traffic through
  `PassthroughCluster`, which STRICT rejects.
- Every Service is ClusterIP. No Ingress, LoadBalancer, NodePort, or external IP.
- Prometheus scrapes only `platformforge-dev`, and only the sidecar annotation port
  15020 (`/stats/prometheus`). Its RoleBinding in that namespace points at
  ClusterRole `platformforge-prometheus`, which can list pods, services, endpoints, and
  endpointslices. It cannot list nodes, secrets, or configmaps. Alertmanager,
  node-exporter, kube-state-metrics, and the default cluster-wide jobs are off.
  Retention is 6h or 512MB, on an emptyDir.
- Loki is one monolithic replica, filesystem on an emptyDir, auth disabled,
  `replication_factor: 1`. The chart's cluster-wide configmap/secret Role is not
  installed (`sidecar.rules.enabled: false`, `rbac.namespaced: true`).
- Alloy is one Deployment. It tails pods through the Kubernetes API
  (`loki.source.kubernetes`) with a Role in `platformforge-dev` for `pods` and
  `pods/log` only. No hostPath and no DaemonSet. Its writable directory is an emptyDir
  at `/tmp/alloy` because the root filesystem is read-only.
- Tempo is one replica, local backend, no metrics generator, no multitenancy. The chart
  still publishes the Jaeger receiver ports on the Service. NetworkPolicy
  `tempo-otlp-only` admits only TCP 3200, 4317, and 4318. From `pf-test-client-plain`,
  connections to 9411 and 14268 timed out, while 4318 and 3200 connected.
- Grafana has no admin password in git. The chart generates Secret `grafana`. Anonymous
  viewers can open dashboards because the Service is ClusterIP on this lab, the same
  trust boundary as Prometheus, Loki, and Tempo. Datasources are Prometheus, Loki, and
  Tempo. Loki derives a Tempo link from `"trace_id":"<id>"`. Tempo's logs query is
  `{namespace="platformforge-dev"} |= "<trace id>"`.
- Istio MeshConfig adds extension provider `otel-tracing` (OTLP gRPC to
  `tempo.platformforge-observability.svc.cluster.local:4317`) and puts `trace_id` in the
  access log. Sampling is not mesh-wide. Telemetry `tracing` in `platformforge-dev`
  sets `randomSamplingPercentage: 100` and `useRequestIdForTraceSampling: false`.
  PlatformForge request IDs are caller-supplied and are not always UUIDs; with the
  default, Envoy hashes `x-request-id` and drops the trace. PeerAuthentication stays
  STRICT and the inventory AuthorizationPolicy is unchanged.

## Ephemeral storage

Prometheus, Loki, Tempo, and Grafana use emptyDir (or the chart's equivalent with
persistence disabled). A pod restart drops metrics, logs, traces, and any Grafana
state that was not provisioned from git. Dashboards and datasource definitions come
from the values files and the `platformforge-dashboards` ConfigMap, so those come
back. Do not treat this cluster as a system of record.

## Security limitations

- Prometheus, Loki, and Tempo do not authenticate callers. Anyone who can open a
  connection inside the cluster can query them.
- Grafana's anonymous role is Viewer. The admin password is only in the generated
  Secret. Do not copy that Secret into git.
- Trace sampling is 100% in `platformforge-dev`. That is a lab choice so a drill
  request can be found. It is not a production rate.
- The observability namespace is Pod Security `baseline`, not `restricted`. The
  upstream charts do not all meet `restricted`. `platformforge-dev` stays `restricted`
  and STRICT.
- Alloy can read pod logs in `platformforge-dev`, including any secret a container
  prints. Application logs are not supposed to contain credentials or request bodies.
- Tempo's Service object still lists the Jaeger ports. The NetworkPolicy is what
  drops them, and that was checked from the plaintext test client on this cluster.
- orders-service has no `spec.version`, so Istio's `destination_version` label for it
  is `unknown`. Setting `version` would rename the Deployment. Inventory versions are
  `v0.1.0` and `v0.2.0`.

## Verification

`tests/integration/test_observability.py` checks scrape health, a real increase in
`istio_requests_total`, Loki lines from both services and an Istio proxy, a Tempo
trace whose inventory span is a descendant of the orders server span, Grafana
datasource health, and the canary drill. Recorded output is in `TEST_EVIDENCE.md`.
The Grafana UI at `/d/platformforge-overview` showed live request-rate series and
8 sidecar targets up.
