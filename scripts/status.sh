#!/usr/bin/env bash
# Health summary for every service in the stack.
set -uo pipefail

PORT_KANBAN=${PORT_KANBAN:-8081}
PORT_RUNTIME=${PORT_RUNTIME:-8082}
PORT_TOOLS=${PORT_TOOLS:-8083}
PORT_OBS=${PORT_OBS:-8084}
PORT_SHELL=${PORT_SHELL:-8085}
PORT_BOARD=${PORT_BOARD:-8086}
PORT_MEMORY=${PORT_MEMORY:-8087}

check() {
  local name="$1" port="$2" path="${3:-/health}"
  local body
  if body=$(curl -fsS --max-time 4 "http://127.0.0.1:$port$path" 2>/dev/null); then
    printf '  %-16s :%-5s UP    %s\n' "$name" "$port" "$(echo "$body" | head -c 110)"
  else
    printf '  %-16s :%-5s DOWN\n' "$name" "$port"
  fi
}

echo "AI-native Kali service status"
check kanban-core   "$PORT_KANBAN"
check agent-runtime "$PORT_RUNTIME"
check tool-frontends "$PORT_TOOLS"
check observability "$PORT_OBS"
check hermes-shell  "$PORT_SHELL"
check board-ui      "$PORT_BOARD"
check memory-store  "$PORT_MEMORY"
