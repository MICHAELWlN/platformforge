#!/bin/bash
# Loopback port-forwards only. Does not change a Service type.
set -euo pipefail
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:${PATH}"
CTX="gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev"
current="$(kubectl config current-context)"
echo "CURRENT=${current}"
if [[ "$current" != "$CTX" ]]; then
  echo "refusing context ${current}" >&2
  exit 2
fi
pidfile="/tmp/pf-demo-pf.pids"
mkdir -p /tmp
: > "$pidfile"
hold="${1:-}"
start() {
  local port="$1"
  local svc="$2"
  local target="$3"
  if curl -fsS "http://127.0.0.1:${port}/ready" >/dev/null 2>&1 || curl -fsS "http://127.0.0.1:${port}/-/ready" >/dev/null 2>&1 || curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1; then
    echo "already listening 127.0.0.1:${port}"
    return
  fi
  if [[ "$hold" == "--wait" ]]; then
    kubectl --context "$CTX" -n platformforge-observability port-forward \
      --address 127.0.0.1,::1 "svc/${svc}" "${port}:${target}" \
      >/tmp/pf-demo-${port}.log 2>&1 &
  else
    nohup kubectl --context "$CTX" -n platformforge-observability port-forward \
      --address 127.0.0.1,::1 "svc/${svc}" "${port}:${target}" \
      >/tmp/pf-demo-${port}.log 2>&1 &
    disown "$!" || true
  fi
  echo $! >> "$pidfile"
  echo "started ${svc} on 127.0.0.1:${port} pid $!"
}
start 9090 prometheus-server 80
start 3100 loki 3100
start 3200 tempo 3200
start 3000 grafana 80
ready() {
  curl -fsS http://127.0.0.1:9090/-/ready >/dev/null \
    && curl -fsS http://127.0.0.1:3100/ready >/dev/null \
    && curl -fsS http://127.0.0.1:3200/ready >/dev/null \
    && curl -fsS http://127.0.0.1:3000/api/health >/dev/null
}
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if ready; then
    echo "telemetry ready"
    if [[ "${1:-}" == "--wait" ]]; then
      echo "waiting until the port-forwards exit"
      wait
    fi
    exit 0
  fi
  sleep 1
done
echo "telemetry port-forwards did not become ready" >&2
exit 1
