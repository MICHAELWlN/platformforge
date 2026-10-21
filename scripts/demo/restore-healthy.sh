#!/bin/bash
# Restore the healthy canary and the reviewed 100/0 route.
# Refuses to run until CONFIRM_RESTORE=yes. Does not delete a Deployment.
set -euo pipefail
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:${PATH}"
if [[ "${CONFIRM_RESTORE:-}" != "yes" ]]; then
  echo "Refusing to restore. Set CONFIRM_RESTORE=yes after you mean to change the canary." >&2
  exit 2
fi
CTX="gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev"
current="$(kubectl config current-context)"
echo "CURRENT=${current}"
if [[ "$current" != "$CTX" ]]; then
  echo "refusing context ${current}" >&2
  exit 2
fi
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
"$root/.venv/bin/python" -m platformforge.gke render \
  --lock deploy/gke/image-lock.yaml \
  services/manifests/inventory-service-v0.2.0.yaml > /tmp/pf-demo-healthy-sm.yaml
"$root/.venv/bin/platformforge" deploy --dry-run /tmp/pf-demo-healthy-sm.yaml > /tmp/pf-demo-healthy-k8s.yaml
if grep -q "LoadBalancer\|NodePort" /tmp/pf-demo-healthy-k8s.yaml; then
  echo "refusing a public Service" >&2
  exit 2
fi
kubectl --context "$CTX" apply -f deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml
kubectl --context "$CTX" apply -f /tmp/pf-demo-healthy-k8s.yaml
kubectl --context "$CTX" -n platformforge-dev rollout status deploy/inventory-service-v0-2-0 --timeout=180s
kubectl --context "$CTX" -n platformforge-dev get deploy inventory-service-v0-2-0 \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INVENTORY_FAILURE_PERCENT")].value}{"\n"}'
