#!/bin/bash
# Stop only the port-forwards this demo started. Does not delete cluster objects.
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
"${script_dir}/connect.sh" stop
pidfile="/tmp/pf-demo-pf.pids"
if [[ ! -f "$pidfile" ]]; then
  echo "no demo port-forward pid file"
  exit 0
fi
while read -r pid; do
  [[ -z "$pid" ]] && continue
  if ps -p "$pid" -o command= | grep -q "port-forward"; then
    kill "$pid" || true
    echo "stopped ${pid}"
  else
    echo "skipped ${pid}"
  fi
done < "$pidfile"
rm -f "$pidfile"
echo "port-forwards stopped"
