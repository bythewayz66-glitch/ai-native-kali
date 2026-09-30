#!/usr/bin/env bash
# Stop every service started by scripts/dev.sh.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

stopped=0
for pidfile in var/run/*.pid; do
  [ -e "$pidfile" ] || continue
  pid="$(cat "$pidfile")"
  name="$(basename "$pidfile" .pid)"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -9 "$pid" 2>/dev/null || true
    echo "  stopped $name (pid $pid)"
    stopped=$((stopped+1))
  fi
  rm -f "$pidfile"
done

if [ "$stopped" -eq 0 ]; then
  echo "  nothing running"
fi
