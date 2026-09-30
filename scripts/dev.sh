#!/usr/bin/env bash
# Boot the whole AI-native Kali stack locally.
#
#   kanban-core       :8081   orchestration backbone (cards, boards, events)
#   agent-runtime     :8082   Kanban <-> CrewAI bridge
#   tool-frontends    :8083   guardrailed Kali tool layer
#   observability     :8084   collector + panels + dashboard
#   hermes-shell      :8085   desktop shell panel
#   board-ui          :8086   interactive drag-and-drop Kanban board (Phase 2)
#   memory-store      :8087   L6 episodic + semantic memory (Phase 2)
#
# Every service runs with the shared PYTHONPATH so `import kanban_core` etc. work
# straight from the source tree - no installation step.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p var/db var/log var/run

PORT_KANBAN=${PORT_KANBAN:-8081}
PORT_RUNTIME=${PORT_RUNTIME:-8082}
PORT_TOOLS=${PORT_TOOLS:-8083}
PORT_OBS=${PORT_OBS:-8084}
PORT_SHELL=${PORT_SHELL:-8085}
PORT_BOARD=${PORT_BOARD:-8086}
PORT_MEMORY=${PORT_MEMORY:-8087}

# All components import each other from the source tree.
export PYTHONPATH="$ROOT/kanban-core:$ROOT/tool-frontends:$ROOT/agent-runtime:$ROOT/observability:$ROOT/hermes-shell:$ROOT/board-ui:$ROOT/memory-store"
export PYTHONUNBUFFERED=1

# --- service wiring -------------------------------------------------------
export KANBAN_DB="$ROOT/var/db/kanban.db"
export KANBAN_SEED=1
export TOOLS_AUDIT_DB="$ROOT/var/db/tool-audit.db"
# Safety default: live tool execution is OFF and no tool is unlocked. Everything
# runs as a dry run until an operator explicitly opens it.
export TOOLS_LIVE=${TOOLS_LIVE:-0}
export TOOLS_UNLOCK=${TOOLS_UNLOCK:-}
export TOOLS_MAX_TIER=${TOOLS_MAX_TIER:-3}

export KANBAN_URL="http://127.0.0.1:$PORT_KANBAN"
export TOOLS_URL="http://127.0.0.1:$PORT_TOOLS"
export RUNTIME_URL="http://127.0.0.1:$PORT_RUNTIME"
export OBS_URL="http://127.0.0.1:$PORT_OBS"
export BRIDGE_INTERVAL=${BRIDGE_INTERVAL:-2.0}
# Phase 2: the bridge claims cards from the event stream, polling only as a
# fallback while the socket is unhealthy.
export BRIDGE_USE_STREAM=${BRIDGE_USE_STREAM:-1}
export KANBAN_WS_URL="ws://127.0.0.1:$PORT_KANBAN/ws/events"
# Phase 3: a local model is opt-in. Unset/0 keeps the deterministic runner.
export MODEL_ENABLED=${MODEL_ENABLED:-0}
export MODEL_BASE_URL=${MODEL_BASE_URL:-http://127.0.0.1:11434}
export MODEL_NAME=${MODEL_NAME:-llama3.1}
export GATE_TIMEOUT=${GATE_TIMEOUT:-120}
export OBS_INTERVAL=${OBS_INTERVAL:-2.0}
export SHELL_POLL_MS=${SHELL_POLL_MS:-3000}
# Phase 2, L6 memory. Off by default so a deployment without it behaves exactly
# as before; the bridge degrades to "no prior context" rather than failing.
# Phase 4: the bridge reads engagement memory (similarity recall) before a crew
# run. Enabled by default in dev so the recall path is exercised by `make smoke`;
# it stays optional in production because a broken store must never block work.
export MEMORY_ENABLED=${MEMORY_ENABLED:-1}
export MEMORY_DB=${MEMORY_DB:-$ROOT/var/db/memory.db}
export MEMORY_ENGAGEMENT=${MEMORY_ENGAGEMENT:-default}
export MEMORY_URL=${MEMORY_URL:-http://127.0.0.1:$PORT_MEMORY}

start() {
  local name="$1" port="$2" module="$3"
  local pidfile="var/run/$name.pid" logfile="var/log/$name.log"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "  $name already running (pid $(cat "$pidfile"))"
    return 0
  fi
  "$PYTHON" -m uvicorn "$module" --host 127.0.0.1 --port "$port" --log-level warning \
    >"$logfile" 2>&1 &
  echo $! > "$pidfile"
  echo "  $name -> :$port (pid $(cat "$pidfile"), log $logfile)"
}

PYTHON=${PYTHON:-python3}

if ! "$PYTHON" -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
  echo "missing dependencies. Run: $PYTHON -m pip install -r requirements-dev.txt" >&2
  exit 1
fi

echo "booting AI-native Kali stack from $ROOT"

# Order matters: tool-frontends has no dependencies, kanban-core is the hub,
# and the bridge/observability/shell all point at it.
start tool-frontends "$PORT_TOOLS" tool_frontends.server:app
start kanban-core   "$PORT_KANBAN" kanban_core.api:app
start observability "$PORT_OBS"    observability.server:app
start agent-runtime "$PORT_RUNTIME" agent_runtime.server:app
start hermes-shell  "$PORT_SHELL"  hermes_shell.server:app
start board-ui      "$PORT_BOARD"  board_ui.server:app
start memory-store  "$PORT_MEMORY" memory_store.server:app

echo "waiting for health..."
# Seven services now; count them rather than hard-coding the number.
HEALTH_PATHS=(
  "$PORT_KANBAN/health" "$PORT_TOOLS/health" "$PORT_OBS/health"
  "$PORT_RUNTIME/health" "$PORT_SHELL/health" "$PORT_BOARD/health"
  "$PORT_MEMORY/health"
)
for i in $(seq 1 60); do
  ok=0
  for p in "${HEALTH_PATHS[@]}"; do
    if curl -fsS "http://127.0.0.1:$p" >/dev/null 2>&1; then ok=$((ok+1)); fi
  done
  if [ "$ok" -eq "${#HEALTH_PATHS[@]}" ]; then
    echo "all ${#HEALTH_PATHS[@]} services healthy"
    echo ""
    echo "  kanban board        http://127.0.0.1:$PORT_BOARD/"
    echo "  hermes shell panel  http://127.0.0.1:$PORT_SHELL/panel"
    echo "  observability       http://127.0.0.1:$PORT_OBS/dashboard"
    echo "  memory store        http://127.0.0.1:$PORT_MEMORY/docs"
    echo "  kanban API docs     http://127.0.0.1:$PORT_KANBAN/docs"
    echo ""
    echo "  run:  make smoke   (proves the full card lifecycle)"
    exit 0
  fi
  sleep 0.5
done

echo "services did not all become healthy in 30s - check var/log/*.log" >&2
exit 1
