#!/bin/bash
# Deploy the faulty canary and the reviewed 90/10 route.
# Refuses to run until CONFIRM_FAILURE=yes. Does not delete the stable Deployment.
set -euo pipefail
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:${PATH}"
if [[ "${CONFIRM_FAILURE:-}" != "yes" ]]; then
  echo "Refusing to inject a failure. Set CONFIRM_FAILURE=yes after you mean to." >&2
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
  services/manifests/incidents/inventory-service-v0.2.0-faulty.yaml > /tmp/pf-demo-faulty-sm.yaml
"$root/.venv/bin/platformforge" deploy --dry-run /tmp/pf-demo-faulty-sm.yaml > /tmp/pf-demo-faulty-k8s.yaml
if grep -q "LoadBalancer\|NodePort" /tmp/pf-demo-faulty-k8s.yaml; then
  echo "refusing a public Service" >&2
  exit 2
fi
if ! grep -q 'INVENTORY_FAILURE_PERCENT' /tmp/pf-demo-faulty-k8s.yaml; then
  echo "faulty manifest lost the failure setting" >&2
  exit 2
fi
kubectl --context "$CTX" apply -f /tmp/pf-demo-faulty-k8s.yaml
kubectl --context "$CTX" -n platformforge-dev rollout status deploy/inventory-service-v0-2-0 --timeout=180s
kubectl --context "$CTX" apply -f deploy/istio/platformforge-dev/inventory-virtualservice-canary.yaml
echo "90/10 applied. The controller does not select this file. Watch its log for baseline recorded, then scripts/demo/traffic.sh."
