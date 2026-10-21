#!/bin/bash
# Read-only demo preflight. Refuses every context other than the pinned GKE context.
set -euo pipefail
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:${PATH}"
CTX="gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev"
current="$(kubectl config current-context)"
echo "CURRENT=${current}"
if [[ "$current" != "$CTX" ]]; then
  echo "refusing context ${current}" >&2
  exit 2
fi
echo "=== nodes ==="
kubectl --context "$CTX" get nodes
echo "=== deployments ==="
kubectl --context "$CTX" -n platformforge-dev get deploy \
  inventory-service-v0-1-0 inventory-service-v0-2-0 orders-service canary-controller
echo "=== route ==="
kubectl --context "$CTX" -n platformforge-dev get virtualservice inventory-service \
  -o jsonpath='{range .spec.http[0].route[*]}{.destination.subset}={.weight}{"\n"}{end}'
echo
echo "=== mtls ==="
kubectl --context "$CTX" -n platformforge-dev get peerauthentication default \
  -o jsonpath='{.spec.mtls.mode}{"\n"}'
echo "=== failure percent ==="
kubectl --context "$CTX" -n platformforge-dev get deploy inventory-service-v0-2-0 \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INVENTORY_FAILURE_PERCENT")].value}{"\n"}'
echo "=== public services ==="
kubectl --context "$CTX" get svc -A --no-headers | awk '$3=="LoadBalancer" || $3=="NodePort" {print}' || true
echo "=== pvc ==="
kubectl --context "$CTX" get pvc -A --no-headers
echo "preflight done"
