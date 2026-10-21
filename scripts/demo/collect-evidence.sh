#!/bin/bash
# Read controller logs and, when the loopback forward is up, Prometheus counters.
# Does not apply a manifest or change a route.
set -euo pipefail
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:${PATH}"
CTX="gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev"
current="$(kubectl config current-context)"
echo "CURRENT=${current}"
if [[ "$current" != "$CTX" ]]; then
  echo "refusing context ${current}" >&2
  exit 2
fi
echo "=== controller log ==="
kubectl --context "$CTX" -n platformforge-dev logs deploy/canary-controller --tail=20
if curl -fsS http://127.0.0.1:9090/-/ready >/dev/null; then
  echo "=== prometheus ==="
  python3 - <<'PY'
import json, urllib.parse, urllib.request
queries = [
    'sum by (destination_version, response_code) (istio_requests_total{reporter="source",destination_service_name="inventory-service"})',
    "platformforge_canary_rollbacks_total",
    "platformforge_canary_state",
    "platformforge_canary_decisions_total",
]
for query in queries:
    url = "http://127.0.0.1:9090/api/v1/query?" + urllib.parse.urlencode({"query": query})
    data = json.load(urllib.request.urlopen(url, timeout=10))
    print(query)
    for row in data["data"]["result"]:
        print(" ", row["metric"], row["value"][1])
PY
else
  echo "Prometheus is not on 127.0.0.1:9090. Start scripts/demo/port-forwards.sh."
fi
