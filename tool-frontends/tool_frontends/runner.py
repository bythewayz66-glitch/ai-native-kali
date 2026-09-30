"""Sandboxed tool execution.

Blueprint ref: section 08 - 'sandboxing of agent-executed exploits'.

Two execution paths:

* **dry-run** (the default) - nothing is executed at all. The command is
  rendered, audited and returned so the operator can see exactly what *would*
  run. This is what makes the whole layer safe by default.
* **live** - the command is executed with ``shell=False`` (argv form, so no
  shell metacharacter can ever be interpreted), under a wall-clock timeout, with
  resource limits applied and ``PATH`` restricted.

Even the live path is deliberately conservative: only the first executable in
the rendered command is invoked, and no shell is involved at any point.
"""
from __future__ import annotations

import os
import resource
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .audit import ToolAuditLog
from .guardrails import evaluate
from .spec import ToolSpec
from kanban_core.models import Scope, utcnow


@dataclass
class SandboxLimits:
    """Resource envelope for a live run."""

    timeout_s: int = 60
    cpu_seconds: int = 30
    max_memory_mb: int = 512
    max_output_bytes: int = 64 * 1024
    workdir: str = "/tmp/kali-ai-sandbox"
    allow_network: bool = True


@dataclass
class ToolResult:
    """Outcome of a (possibly dry-run) tool invocation."""

    tool: str
    tier: int
    status: str  # ok | error | denied | blocked | dry_run
    dry_run: bool
    allowed: bool
    command: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    duration_ms: int = 0
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    audit_hash: Optional[str] = None
    audit_seq: Optional[int] = None
    ts: str = field(default_factory=utcnow)
    available: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "tier": self.tier,
            "status": self.status,
            "dry_run": self.dry_run,
            "allowed": self.allowed,
            "command": self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "reasons": self.reasons,
            "checks": self.checks,
            "audit_hash": self.audit_hash,
            "audit_seq": self.audit_seq,
            "ts": self.ts,
            "available": self.available,
        }

    def to_trace(self, *, agent: Optional[str] = None) -> dict[str, Any]:
        """Shape this result as a Kanban card trace (blueprint 03.2)."""
        return {
            "tool": self.tool,
            "tier": self.tier,
            "status": "ok" if self.status in ("ok", "dry_run") else self.status,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout[-500:],
            "stderr_tail": self.stderr[-500:],
            "dry_run": self.dry_run,
            "audit_hash": self.audit_hash,
            "agent": agent,
        }


def _limit_resources(cpu_seconds: int, max_memory_mb: int):
    """preexec_fn: applied in the child process, before exec."""

    def _apply() -> None:  # pragma: no cover - runs in the forked child
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        except Exception:
            pass
        try:
            limit = max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except Exception:
            pass
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        except Exception:
            pass

    return _apply


def run_tool(
    spec: ToolSpec,
    args: Optional[dict[str, Any]] = None,
    *,
    scope: Optional[Scope | dict[str, Any]] = None,
    approved: bool = False,
    live: bool = False,
    live_unlocked: bool = False,
    caller: str = "agent",
    card_id: Optional[str] = None,
    audit: Optional[ToolAuditLog] = None,
    limits: Optional[SandboxLimits] = None,
    force_dry_run: Optional[bool] = None,
) -> ToolResult:
    """Validate, audit and (optionally) execute one tool invocation."""
    args = {**spec.defaults(), **(args or {})}
    limits = limits or SandboxLimits(timeout_s=spec.timeout_s)
    #: An explicit force_dry_run (used by tests and the safe-mode toggle) wins.
    if force_dry_run is True:
        live = False

    # -- argument validation -------------------------------------------------
    # NOTE: a live request that is not unlocked is deliberately passed through to
    # the guardrail engine so it comes back as an explicit *denial*. Silently
    # downgrading it to a dry run would let a caller believe it had scanned a
    # host for real when nothing happened - the failure mode blueprint section 08
    # exists to prevent.
    problems = spec.validate_args(args)
    decision = evaluate(
        spec,
        args,
        scope=scope,
        approved=approved,
        live=live,
        live_unlocked=live_unlocked,
        caller=caller,
    )
    reasons = list(decision.reasons)
    if problems:
        allowed = False
        status = "denied"
        reasons = problems + reasons
    else:
        allowed = decision.allowed
        status = decision.status

    # -- render the command --------------------------------------------------
    template = spec.live_template if live else spec.dry_run_template
    if not template:
        template = spec.dry_run_template or spec.live_template
    try:
        command = spec.render(template, args)
    except ValueError as exc:
        allowed, status, command = False, "denied", ""
        reasons.append(str(exc))

    # -- the tool binary may simply not exist on this host -------------------
    available = shutil.which(spec.binary) is not None

    # -- refused: audit the refusal and stop ---------------------------------
    if not allowed:
        entry = None
        if audit is not None:
            entry = audit.append(
                ts=utcnow(),
                tool=spec.name,
                tier=spec.tier,
                status="denied",
                dry_run=not live,
                caller=caller,
                card_id=card_id,
                target=decision.checks.get("target"),
                decision=status,
                command=command,
                args=args,
                reasons=reasons,
            )
        return ToolResult(
            tool=spec.name,
            tier=spec.tier,
            status="denied",
            dry_run=not live,
            allowed=False,
            command=command,
            reasons=reasons,
            checks=decision.checks,
            audit_hash=(entry or {}).get("audit_hash"),
            audit_seq=(entry or {}).get("seq"),
            available=available,
        )

    # -- dry-run: describe, never execute ------------------------------------
    if not live:
        entry = None
        if audit is not None:
            entry = audit.append(
                ts=utcnow(),
                tool=spec.name,
                tier=spec.tier,
                status="dry_run",
                dry_run=True,
                caller=caller,
                card_id=card_id,
                target=decision.checks.get("target"),
                decision="allowed",
                command=command,
                args=args,
                reasons=[],
            )
        return ToolResult(
            tool=spec.name,
            tier=spec.tier,
            status="dry_run",
            dry_run=True,
            allowed=True,
            command=command,
            stdout=f"[dry-run] would execute: {command}",
            reasons=[],
            checks=decision.checks,
            audit_hash=(entry or {}).get("audit_hash"),
            audit_seq=(entry or {}).get("seq"),
            available=available,
        )

    # -- live: execute ------------------------------------------------------ 
    limits.workdir = limits.workdir if limits.workdir else "/tmp"
    try:
        os.makedirs(limits.workdir, exist_ok=True)  # noqa: PTH103
    except Exception:
        limits.workdir = "/tmp"

    argv = shlex.split(command)
    started = time.monotonic()
    stdout = stderr = ""
    exit_code: Optional[int] = None
    run_status = "ok"

    if not available:
        # Nothing to run. Record it honestly rather than inventing output.
        run_status = "blocked"
        stderr = f"binary '{spec.binary}' is not installed on this host"
        duration_ms = 0
    else:
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": limits.workdir,
            "LANG": "C.UTF-8",
        }
        try:
            proc = subprocess.run(
                argv,
                cwd=limits.workdir,
                env=env,
                shell=False,
                capture_output=True,
                timeout=limits.timeout_s,
                text=True,
                preexec_fn=_limit_resources(limits.cpu_seconds, limits.max_memory_mb),
            )
            stdout = (proc.stdout or "")[: limits.max_output_bytes]
            stderr = (proc.stderr or "")[: limits.max_output_bytes]
            exit_code = proc.returncode
            run_status = "ok" if proc.returncode == 0 else "error"
        except subprocess.TimeoutExpired:
            run_status = "error"
            stderr = f"timeout after {limits.timeout_s}s"
            exit_code = -9
        except (FileNotFoundError, PermissionError) as exc:
            run_status = "blocked"
            stderr = str(exc)
            exit_code = -1
        except Exception as exc:  # pragma: no cover - defensive
            run_status = "error"
            stderr = f"sandbox failure: {exc}"
            exit_code = -1
        duration_ms = int((time.monotonic() - started) * 1000)

    entry = None
    if audit is not None:
        entry = audit.append(
            ts=utcnow(),
            tool=spec.name,
            tier=spec.tier,
            status=run_status,
            dry_run=False,
            caller=caller,
            card_id=card_id,
            target=decision.checks.get("target"),
            decision="allowed",
            command=command,
            duration_ms=duration_ms,
            exit_code=exit_code,
            args=args,
            reasons=[],
        )

    return ToolResult(
        tool=spec.name,
        tier=spec.tier,
        status=run_status,
        dry_run=False,
        allowed=True,
        command=command,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_ms=duration_ms,
        checks=decision.checks,
        audit_hash=(entry or {}).get("audit_hash"),
        audit_seq=(entry or {}).get("seq"),
        available=available,
    )
