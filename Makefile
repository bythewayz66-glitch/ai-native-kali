.PHONY: help dev stop test test-unit test-js smoke demo clean logs status iso iso-full build verify-build mcp memory gtk-client

PY := python3
PORT_KANBAN := 8081
PORT_RUNTIME := 8082
PORT_TOOLS := 8083
PORT_OBS := 8084
PORT_SHELL := 8085
PORT_BOARD := 8086
PORT_MEMORY := 8087

help:
	@echo "AI-native Kali - working scaffold (Phases 1-3 + Phase 4 kickoff)"
	@echo ""
	@echo "  make dev          boot the kanban-core + tool-frontends + agent-runtime + observability + hermes-shell + board-ui + memory-store"
	@echo "  make stop         stop every service started by 'make dev'"
	@echo "  make status       health of all seven services"
	@echo "  make logs         tail the service logs"
	@echo "  make test         run the full test suite (all components)"
	@echo "  make test-js      run the board's pure drag-and-drop logic under node"
	@echo "  make mcp          drive the tool layer over the MCP stdio transport"
	@echo "  make smoke        run the end-to-end smoke test against a running stack"
	@echo "  make demo         dev + smoke, in one shot"
	@echo "  make build        build the metapackages + chroot overlay (runnable here)"
	@echo "  make verify-build re-verify the last build against its manifest"
	@echo "  make iso-full     full live-build ISO build (needs live-build + xorriso + root)"
	@echo "  make gtk-client   report whether the GTK4/Wayland panel client can start here"
	@echo "  make clean        stop services and delete local data"
	@echo ""
	@echo "  kanban board        http://127.0.0.1:$(PORT_BOARD)/"
	@echo "  hermes shell panel  http://127.0.0.1:$(PORT_SHELL)/panel"
	@echo "  observability       http://127.0.0.1:$(PORT_OBS)/dashboard"
	@echo "  memory store        http://127.0.0.1:$(PORT_MEMORY)/docs"
	@echo "  kanban API docs     http://127.0.0.1:$(PORT_KANBAN)/docs"

dev:
	@bash scripts/dev.sh

stop:
	@bash scripts/stop.sh

status:
	@bash scripts/status.sh

logs:
	@tail -n 40 -f var/log/*.log

test:
	@$(PY) -m pytest kanban-core tool-frontends agent-runtime observability hermes-shell board-ui memory-store tests -p no:cacheprovider

test-unit: test

# Drive the tool layer over the real MCP stdio transport (item 2).
mcp:
	@PYTHONPATH=tool-frontends:kanban-core $(PY) tool-frontends/examples/mcp_client.py

# L6 memory store: run the service on its own for manual poking.
memory:
	@PYTHONPATH=memory-store MEMORY_DB=var/memory.db $(PY) -m uvicorn memory_store.server:app --host 127.0.0.1 --port 8087

# The board's drag-and-drop decisions are pure JS - test them under Node too.
test-js:
	@node --test board-ui/tests/test_board_logic.mjs

smoke:
	@$(PY) scripts/smoke_test.py --verbose

demo: dev
	@$(PY) scripts/smoke_test.py --verbose

# =============================================================================
# Build pipeline (Phase 4)
# =============================================================================
# `make build` is the part that genuinely runs here: real .deb metapackages, a
# real chroot overlay, and a manifest whose hashes are checked against the files.
# `make iso-full` is the part that cannot: it needs live-build, xorriso and root,
# and it fails loudly rather than pretending. See docs/BUILD_PIPELINE.md.
build:
	@bash packaging/build.sh

verify-build:
	@$(PY) scripts/verify_build.py var/build

iso: build
	@echo "staged build done. For a bootable ISO: make iso-full"

iso-full:
	@bash packaging/build-iso.sh

# Report whether this host can start the real Wayland/GTK panel client (Phase 6
# item 5). Exits non-zero off a Wayland session, which is the honest answer.
gtk-client:
	@PYTHONPATH=hermes-shell $(PY) -c "import json,sys; from hermes_shell.gtk.client import self_check; c=self_check(); print(json.dumps(c, indent=2)); sys.exit(0 if c['runnable'] else 2)"

clean:
	@bash scripts/stop.sh || true
	@rm -rf var/db var/log var/run var/build
	@echo "cleaned var/db var/log var/run var/build"
