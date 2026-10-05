#!/usr/bin/env python3
"""Live CrewAI end-to-end run against the bundled local model.

This is the "close the end-to-end gap" step: it runs a *real* crewai Crew
(crewai 1.15.23, installed) whose agents are backed by the bundled
``qwen2.5:3b-instruct-q4_K_M`` model on the local Ollama endpoint, with the
card's own tools bound through the adapter's guardrailed executor.

It is deliberately a script rather than a test: a live LLM run is slow and
non-deterministic, so it does not belong in the CI suite. What it proves is that
the adapter's crewai branch works against the *real* package - not the fake
module the unit tests inject.

Run:  PYTHONPATH=agent-runtime python3 scripts/live_crewai_run.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent-runtime"))

from agent_runtime.crewai_adapter import CrewAIAdapter, crewai_available, crewai_version  # noqa: E402
from agent_runtime.crews import get_crew  # noqa: E402
from agent_runtime.roles import get_role  # noqa: E402

SCOPE = {"targets": ["scanme.nmap.org"], "authorization_ref": "TICKET-LIVE"}


def _executor(tool, args, *, scope=None, approved=False, card_id=None):
    """A recording executor that stands in for the tool-frontends service.

    It returns the same shape the real guardrailed executor returns, so the
    adapter's recording path is exercised exactly as in production - the only
    thing stubbed is the network hop to the tool service.
    """
    return {
        "tool": tool,
        "status": "dry_run",
        "dry_run": True,
        "duration_ms": 3,
        "command": f"{tool} {args.get('target', '')}".strip(),
        "audit_hash": f"live-{tool}",
        "reasons": [],
        "stdout": f"[dry-run] would execute: {tool}",
        "stderr": "",
    }


def main() -> int:
    print("=== crewai availability ===")
    print("available:", crewai_available())
    print("version:  ", crewai_version())
    if not crewai_available():
        print("crewai is not importable; cannot run the live path")
        return 2

    model = os.environ.get("CREWAI_LLM_MODEL", "ollama/qwen2.5:3b-instruct-q4_K_M")
    base = os.environ.get("CREWAI_LLM_BASE_URL", "http://127.0.0.1:11434")
    print("llm model:", model, "base:", base)

    card = {
        "card_id": "crd_live",
        "title": "Live recon on the authorised host",
        "description": "Run the card's bound recon tools through a real crewai crew.",
        "scope": SCOPE,
        "crew": "recon",
        "tools": [
            {"name": "whois_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
            {"name": "dns_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
        ],
    }

    adapter = CrewAIAdapter(
        tool_executor=_executor,
        prefer_real=True,
        verbose=False,
        llm_model=model,
        llm_base_url=base,
    )
    print("adapter backend:", adapter.backend)
    print("adapter llm:    ", type(adapter.llm).__name__ if adapter.llm else None)

    started = time.monotonic()
    result = adapter.run(get_crew("recon"), card, role_lookup=get_role, scope=SCOPE)
    elapsed = time.monotonic() - started

    print("\n=== live crewai run result ===")
    print("backend:  ", result.backend)
    print("status:   ", result.status)
    print("duration: ", f"{elapsed:.1f}s")
    print("steps:    ", len(result.steps))
    for step in result.steps:
        print(f"  - {step.role}: {step.summary}")
    print("findings: ", result.findings)
    print("errors:   ", result.errors)
    print("\nsummary:\n", result.summary[:1500])

    out = {
        "backend": result.backend,
        "status": result.status,
        "duration_s": round(elapsed, 1),
        "steps": [s.__dict__ for s in result.steps],
        "findings": result.findings,
        "errors": result.errors,
        "crewai_version": crewai_version(),
        "llm_model": model,
    }
    with open("/workspace/live_crewai_result.json", "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\nwrote /workspace/live_crewai_result.json")

    # The live path is only "closed" if crewai actually ran (not the fallback).
    return 0 if result.backend == "crewai" else 1


if __name__ == "__main__":
    raise SystemExit(main())
