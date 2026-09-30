# AI-native Kali - development image.
#
# Single image for all six services: they share the source tree through a common
# PYTHONPATH, which is exactly how `make dev` runs them on the host.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# nmap/nikto/dig/whois are NOT installed: the guardrail layer runs in dry-run by
# default and never needs them. Installing offensive tooling into a dev image
# would be a liability with no benefit, so live execution on this image is a
# deliberate no-op. The real ISO ships the Kali toolchain.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: this layer only rebuilds when requirements change.
COPY requirements-dev.txt ./
RUN python3 -m pip install -r requirements-dev.txt

# Then the source tree.
COPY kanban-core/      kanban-core/
COPY tool-frontends/   tool-frontends/
COPY agent-runtime/    agent-runtime/
COPY observability/    observability/
COPY hermes-shell/     hermes-shell/
COPY board-ui/         board-ui/
COPY packaging/        packaging/
COPY docs/             docs/
COPY scripts/          scripts/
COPY Makefile conftest.py pytest.ini README.md BUILD_STATUS.md ./

ENV PYTHONPATH=/app/kanban-core:/app/tool-frontends:/app/agent-runtime:/app/observability:/app/hermes-shell:/app/board-ui

# Shared SQLite lives on a volume so the stack survives a rebuild.
RUN mkdir -p /data

# Default service; compose overrides the command per container.
EXPOSE 8081 8082 8083 8084 8085 8086
CMD ["python3", "-m", "uvicorn", "kanban_core.api:app", "--host", "0.0.0.0", "--port", "8081"]
