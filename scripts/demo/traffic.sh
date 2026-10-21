#!/bin/bash
# Send orders checks through the existing mesh client. Does not change routing.
# Refuses to run until CONFIRM_TRAFFIC=yes.
set -euo pipefail
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:${PATH}"
if [[ "${CONFIRM_TRAFFIC:-}" != "yes" ]]; then
  echo "Refusing to send traffic. Set CONFIRM_TRAFFIC=yes after you mean to." >&2
  exit 2
fi
CTX="gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev"
current="$(kubectl config current-context)"
echo "CURRENT=${current}"
if [[ "$current" != "$CTX" ]]; then
  echo "refusing context ${current}" >&2
  exit 2
fi
count="${1:-20}"
if ! [[ "$count" =~ ^[0-9]+$ ]] || (( count < 1 || count > 400 )); then
  echo "count must be 1 to 400" >&2
  exit 2
fi
prefix="demo-$(date +%s)"
kubectl --context "$CTX" -n platformforge-dev exec pf-test-client-mesh -c client -- \
  /usr/bin/python3.13 -c "
import json, urllib.request, urllib.error
from collections import Counter
c = Counter()
url = 'http://orders-service.platformforge-dev.svc.cluster.local:8080/orders/check/widget'
for i in range(${count}):
    req = urllib.request.Request(url, headers={'x-request-id': '${prefix}-%s' % i})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            c[str(resp.status)] += 1
    except urllib.error.HTTPError as exc:
        c[str(exc.code)] += 1
print(json.dumps(dict(c)))
"
