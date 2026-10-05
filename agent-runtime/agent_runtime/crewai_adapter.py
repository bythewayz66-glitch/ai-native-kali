"""CrewAI adapter: real CrewAI when installed, deterministic runtime otherwise.

Blueprint ref: section 06. The bridge never talks to CrewAI directly; it talks to
this adapter, which has two backends:

* **crewai** - used when the ``crewai`` package is importable. Real ``Agent`` /
  ``Task`` / ``Crew`` objects are constructed with our role contracts and the
  card's own bound tools attached as callables, so a crewai run reaches the same
  guardrailed tool layer (and the same hash-chained audit log) as every other
  path.
* **local** - a deterministic, dependency-free executor. Same ordering, same
  tool calls, same trace shapes, so the whole loop (and every test) runs on a
  laptop with no LLM keys and no network.

Both backends return the same :class:`CrewRunResult`, so nothing downstream can
tell which one ran - which is exactly what makes the local backend a legitimate
CI target rather than a mock.

**No hard dependency.** ``crewai`` is imported lazily, inside the methods that
need it, and never at module import time. A host without it imports this module,
constructs the adapter, and runs the deterministic backend exactly as before;
:func:`crewai_available` and :func:`crewai_version` are the only places that
touch the package, and both swallow their own import errors. That is what keeps
CI key-free and GPU-free while still letting a real install switch the backend on
with no code change.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .crews import CrewDef
from .roles import AgentRole


def _crewai_tool_decorator(crewai: Any) -> Any:
    """The ``@tool`` decorator, wherever the installed crewai keeps it.

    crewai 0.x exposed ``crewai.tool``; 1.x moved it to ``crewai.tools.tool``.
    Resolving it here rather than at the call site means the adapter works with
    both, instead of raising ``AttributeError: module 'crewai' has no attribute
    'tool'`` on a modern install - which is exactly what a real ``pip install
    crewai`` produced.
    """
    decorator = getattr(crewai, "tool", None)
    if decorator is not None:
        return decorator
    try:
        from crewai.tools import tool as _tool

        return _tool
    except Exception:
        return None


def crewai_available() -> bool:
    """Is the real CrewAI package importable in this environment?"""
    try:  # pragma: no cover - depends on the host
        import crewai  # noqa: F401

        return True
    except Exception:
        return False


def crewai_version() -> Optional[str]:
    """The installed CrewAI version, or ``None`` when it is not importable.

    Reported on ``/health`` so an operator can tell *which* crewai a run used
    rather than only that one was present - a version skew is the usual cause of
    an adapter that imports but misbehaves.

    Gated on :func:`crewai_available` first, so the answer agrees with the
    backend the adapter would actually pick. Reading distribution metadata
    unconditionally would report a version even when the package cannot be
    imported (a broken install, or a test that blocks the module), which is
    exactly the "imports but misbehaves" case this is meant to expose.
    """
    if not crewai_available():
        return None
    try:
        from importlib.metadata import version

        return version("crewai")
    except Exception:
        pass
    try:  # pragma: no cover - only when the package lacks dist metadata
        import crewai

        return getattr(crewai, "__version__", None)
    except Exception:
        return None


@dataclass
class StepResult:
    """What one role did during a crew run."""

    role: str
    summary: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    status: str = "ok"
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class CrewRunResult:
    """Full outcome of running one crew against one card."""

    crew: str
    backend: str
    status: str  # ok | partial | blocked | failed
    summary: str = ""
    steps: list[StepResult] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)

    #: The recall trail the crew was given: which records were retrieved by
    #: similarity, from which backend, and any retrieval errors. ``None`` when
    #: memory is disabled - which is a different thing from "nothing relevant
    #: was found", and the two are reported distinctly.
    memory: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "crew": self.crew,
            "backend": self.backend,
            "status": self.status,
            "summary": self.summary,
            "steps": [s.__dict__ for s in self.steps],
            "findings": self.findings,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
            "memory": self.memory,
        }


class CrewAIAdapter:
    """Runs a :class:`CrewDef` against a card's bound tools.

    ``tool_executor`` is a callable ``(tool_name, args) -> dict`` supplied by the
    bridge (it performs the real, guardrailed call against the tool-frontends
    service). Keeping it injected is what lets the same adapter run in tests
    against an in-process tool registry.
    """

    def __init__(
        self,
        *,
        tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
        prefer_real: bool = True,
        verbose: bool = False,
        llm_model: Optional[str] = None,
        llm_base_url: Optional[str] = None,
    ) -> None:
        self.tool_executor = tool_executor
        self.prefer_real = prefer_real
        self.verbose = verbose
        #: The model the crewai agents use. Defaults to the bundled instruct
        #: model on the local Ollama endpoint, but overridable so a host can
        #: point the live run at whatever it actually has (a different tag, a
        #: remote endpoint) without editing the adapter.
        self.llm_model = llm_model or os.environ.get("CREWAI_LLM_MODEL", "ollama/llama3.1")
        self.llm_base_url = llm_base_url or os.environ.get(
            "CREWAI_LLM_BASE_URL", "http://127.0.0.1:11434"
        )
        #: Lazily resolved: ``None`` until the first backend query, then either
        #: the imported module or ``None``. Never imported at module scope.
        self._crewai: Any = None
        self._crewai_checked = False
        #: Tool calls recorded during the current crewai run, so the result can
        #: carry the same per-call detail (status, audit hash, command) the
        #: deterministic backend records. Reset at the start of every run.
        self._last_calls: list[dict[str, Any]] = []
        #: Per-request timeout and completion cap for the crewai LLM. A CPU-only
        #: host running a small model can take minutes per completion, and
        #: crewai's default request timeout is far shorter than that - so the
        #: live path would time out and fall back even though the model is
        #: healthy. Both are overridable from the environment so a slow host can
        #: raise them without editing the adapter.
        self.llm_timeout = float(os.environ.get("CREWAI_LLM_TIMEOUT", "600"))
        self.llm_max_tokens = int(os.environ.get("CREWAI_LLM_MAX_TOKENS", "512"))
        self.llm = None
        if self.backend == "crewai":  # pragma: no cover - needs the real package
            try:
                from crewai import LLM

                self.llm = LLM(
                    model=self.llm_model,
                    base_url=self.llm_base_url,
                    timeout=self.llm_timeout,
                    max_tokens=self.llm_max_tokens,
                )
            except Exception:
                self.llm = None

    # -- backend selection ----------------------------------------------
    def _crewai_module(self) -> Any:
        """Import crewai once, on demand. Returns the module or ``None``."""
        if not self._crewai_checked:
            self._crewai_checked = True
            try:
                import crewai

                self._crewai = crewai
            except Exception:
                self._crewai = None
        return self._crewai

    @property
    def backend(self) -> str:
        """``"crewai"`` when the real package is usable, else ``"local"``.

        A property rather than a value frozen in ``__init__`` so that a test (or
        a late ``pip install``) that makes crewai importable is reflected without
        rebuilding the adapter.
        """
        if self.prefer_real and self._crewai_module() is not None:
            return "crewai"
        return "local"

    @property
    def crewai_available(self) -> bool:
        """Can this adapter actually run the crewai backend right now?"""
        return self.backend == "crewai"

    # -- public API ------------------------------------------------------
    def run(
        self,
        crew: CrewDef,
        card: dict[str, Any],
        *,
        role_lookup: Optional[Callable[[str], Optional[AgentRole]]] = None,
        scope: Optional[dict[str, Any]] = None,
        approved: bool = False,
    ) -> CrewRunResult:
        """Execute the crew's pipeline against one card.

        ``scope`` and ``approved`` come from the card and are forwarded to every
        tool call, so the guardrail engine evaluates the same authorization the
        card carries rather than an unscoped request.
        """
        started = time.monotonic()
        if self.backend == "crewai":
            try:
                return self._run_crewai(
                    crew, card, role_lookup, started, scope=scope, approved=approved
                )
            except Exception as exc:
                # Never lose the card because the LLM backend misbehaved.
                fallback = self._run_local(
                    crew, card, role_lookup, started, scope=scope, approved=approved
                )
                fallback.errors.append(f"crewai backend failed, fell back to local: {exc}")
                fallback.backend = "local (fallback)"
                return fallback
        return self._run_local(crew, card, role_lookup, started, scope=scope, approved=approved)

    def run_plan(
        self,
        crew: CrewDef,
        card: dict[str, Any],
        plan: list[dict[str, Any]],
        *,
        role_lookup: Optional[Callable[[str], Optional[AgentRole]]] = None,
        scope: Optional[dict[str, Any]] = None,
        approved: bool = False,
    ) -> CrewRunResult:
        """Execute an **already-validated** plan through crewai agents.

        This is the seam the model path uses. :class:`~agent_runtime.llm_crew.LLMCrewRunner`
        owns validation (permitted set, role ceiling, scope, human gate); once a
        step has passed all of that, this method hands the *chosen* tool to a
        crewai agent to actually run. The plan is never re-interpreted here - the
        tool name and arguments are exactly the validated ones - so crewai cannot
        widen what the validator allowed.

        Each plan entry is ``{"role", "tool", "args", "rationale", "approved"}``.
        ``approved`` is per-step because the human gate decides per step; a step
        the gate approved must not be downgraded to unapproved just because it
        shares a crew with one that was not.

        Raises ``RuntimeError`` when crewai is not importable, so the caller can
        fall back to the deterministic executor rather than silently doing
        nothing.
        """
        crewai = self._crewai_module()
        if crewai is None:
            raise RuntimeError("crewai is not importable")
        Agent = getattr(crewai, "Agent")
        Task = getattr(crewai, "Task")
        Crew = getattr(crewai, "Crew")
        Process = getattr(crewai, "Process")
        crewai_tool = _crewai_tool_decorator(crewai)

        started = time.monotonic()
        self._last_calls = []
        agents: list[Any] = []
        tasks: list[Any] = []
        for proposal in plan:
            tool_name = proposal.get("tool")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            role_name = proposal.get("role") or ""
            args = dict(proposal.get("args") or {})
            step_approved = bool(proposal.get("approved", approved))
            role = role_lookup(role_name) if role_lookup else None
            bound = [
                self._make_tool(
                    tool_name, args, role_name, scope, step_approved, card.get("card_id"), crewai_tool
                )
            ]
            agents.append(
                Agent(
                    role=role.display_name if role else (role_name or "agent"),
                    goal=role.goal if role else f"Run {tool_name} as planned.",
                    backstory=role.backstory if role else "",
                    tools=bound,
                    llm=self.llm,
                    verbose=self.verbose,
                )
            )
            tasks.append(
                Task(
                    description=(
                        f"Run the tool '{tool_name}' with arguments {json.dumps(args, default=str)} "
                        f"on card '{card.get('title')}'."
                        + (f"\nRationale: {proposal['rationale']}" if proposal.get("rationale") else "")
                    ),
                    expected_output="The tool's result, plus any finding worth escalating.",
                    agent=agents[-1],
                )
            )
        if not tasks:
            return CrewRunResult(
                crew=crew.name,
                backend="crewai",
                status="blocked",
                summary="no executable plan steps",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        crew_obj = Crew(agents=agents, tasks=tasks, process=Process.sequential, verbose=self.verbose)
        output = crew_obj.kickoff()
        return self._result_from_calls(crew, card, started, output, backend="crewai")

    # -- backend 1: real CrewAI ------------------------------------------
    def _run_crewai(
        self,
        crew_def: CrewDef,
        card: dict[str, Any],
        role_lookup: Optional[Callable[[str], Optional[AgentRole]]],
        started: float,
        *,
        scope: Optional[dict[str, Any]] = None,
        approved: bool = False,
    ) -> CrewRunResult:
        crewai = self._crewai_module()
        if crewai is None:  # pragma: no cover - guarded by the caller
            raise RuntimeError("crewai is not importable")
        Agent = getattr(crewai, "Agent")
        Task = getattr(crewai, "Task")
        Crew = getattr(crewai, "Crew")
        Process = getattr(crewai, "Process")
        crewai_tool = _crewai_tool_decorator(crewai)

        card_tools = {t.get("name"): t for t in (card.get("tools") or []) if isinstance(t, dict)}
        self._last_calls = []
        agents: list[Any] = []
        tasks: list[Any] = []
        for step in crew_def.steps:
            role = role_lookup(step.role) if role_lookup else None
            bound = self._bind_tools(
                step.tools, card_tools, step.role, scope, approved, card.get("card_id"), crewai_tool
            )
            agents.append(
                Agent(
                    role=role.display_name if role else step.role,
                    goal=role.goal if role else step.description,
                    backstory=role.backstory if role else "",
                    tools=bound,
                    llm=self.llm,
                    verbose=self.verbose,
                )
            )
            tasks.append(
                Task(
                    description=self._task_description(step, card),
                    expected_output="A short summary of what was found, plus any findings worth escalating.",
                    agent=agents[-1],
                )
            )
        crew = Crew(agents=agents, tasks=tasks, process=Process.sequential, verbose=self.verbose)
        output = crew.kickoff()
        return self._result_from_calls(crew_def, card, started, output, backend="crewai")

    # -- tool binding ----------------------------------------------------
    def _bind_tools(
        self,
        tool_names: list[str],
        card_tools: dict[str, dict[str, Any]],
        role_name: str,
        scope: Optional[dict[str, Any]],
        approved: bool,
        card_id: Optional[str],
        crewai_tool: Callable[..., Any],
    ) -> list[Any]:
        """Bind the card's own tools to a crewai agent.

        Only tools the **card** carries are bound, and each is bound with the
        card's arguments. That is deliberate: a crewai agent that could call a
        tool the card never bound would be a way around the guardrail engine,
        which authorizes the *card's* request. A tool the crew lists but the card
        does not bind is simply not offered to the agent.
        """
        bound: list[Any] = []
        for tool_name in tool_names:
            if tool_name not in card_tools:
                continue
            fixed = dict(card_tools[tool_name].get("args") or {})
            bound.append(
                self._make_tool(tool_name, fixed, role_name, scope, approved, card_id, crewai_tool)
            )
        return bound

    def _make_tool(
        self,
        name: str,
        fixed_args: dict[str, Any],
        role_name: str,
        scope: Optional[dict[str, Any]],
        approved: bool,
        card_id: Optional[str],
        crewai_tool: Callable[..., Any],
    ) -> Any:
        """Build one crewai tool that calls our guardrailed executor.

        The card's arguments are merged *under* whatever the agent supplies, so
        the agent can refine a call but cannot drop the card's target. Every call
        is recorded on the adapter so the run result carries the same per-call
        detail (status, audit hash, command) the deterministic backend records -
        without that, a crewai run would be the one run whose trace could not be
        joined back to the audit log.
        """
        adapter = self

        @crewai_tool(name)
        def _call(**kwargs: Any) -> str:
            """Invoke an AI-native Kali tool frontend through the guardrail engine."""
            merged = {**fixed_args, **kwargs}
            outcome = adapter.tool_executor(
                name, merged, scope=scope, approved=approved, card_id=card_id
            )
            adapter._record_call(role_name, name, outcome)
            return json.dumps(outcome, default=str)

        return _call

    def _record_call(self, role_name: str, tool_name: str, outcome: dict[str, Any]) -> None:
        self._last_calls.append(
            {
                "role": role_name,
                "tool": tool_name,
                "status": outcome.get("status", "unknown"),
                "dry_run": outcome.get("dry_run", True),
                "duration_ms": outcome.get("duration_ms", 0),
                "audit_hash": outcome.get("audit_hash"),
                "command": outcome.get("command", ""),
                "reasons": outcome.get("reasons") or [],
            }
        )

    def _result_from_calls(
        self,
        crew_def: CrewDef,
        card: dict[str, Any],
        started: float,
        output: Any,
        *,
        backend: str,
    ) -> CrewRunResult:
        """Turn the recorded tool calls into a :class:`CrewRunResult`.

        The status is derived from the calls, not assumed: a crewai run whose
        every tool was refused by the guardrails must report ``blocked``, exactly
        as the deterministic backend would. Reporting ``ok`` because the LLM
        finished talking would be the single most misleading thing this adapter
        could do.
        """
        calls_by_role: dict[str, list[dict[str, Any]]] = {}
        for call in self._last_calls:
            calls_by_role.setdefault(call["role"], []).append(call)

        steps: list[StepResult] = []
        findings: list[str] = []
        errors: list[str] = []
        ran_any = False
        for step in crew_def.steps:
            calls = calls_by_role.get(step.role, [])
            step_errors: list[str] = []
            for call in calls:
                if call["status"] in ("ok", "dry_run"):
                    ran_any = True
                elif call["status"] == "denied":
                    step_errors.append(
                        f"{call['tool']} refused by guardrails: {'; '.join(call.get('reasons') or [])}"
                    )
                elif call["status"] == "blocked":
                    step_errors.append(f"{call['tool']} could not run")
            findings.extend(self._findings_from(calls))
            errors.extend(step_errors)
            steps.append(
                StepResult(
                    role=step.role,
                    summary=self._summarise_step(step.role, calls, errors=step_errors),
                    tool_calls=calls,
                    status="error" if step_errors and not calls else "ok",
                )
            )

        status = "ok" if ran_any and not errors else ("partial" if ran_any else "blocked")
        summary = self._summarise_crew(crew_def, card, steps, findings)
        if output is not None:
            text = str(output).strip()
            if text:
                summary = f"{summary}\n\nCrewAI output: {text[:2000]}"
        return CrewRunResult(
            crew=crew_def.name,
            backend=backend,
            status=status,
            summary=summary,
            steps=steps,
            findings=findings,
            duration_ms=int((time.monotonic() - started) * 1000),
            errors=errors,
        )

    @staticmethod
    def _task_description(step: Any, card: dict[str, Any]) -> str:
        return (
            f"{step.description}\n\nCard: {card.get('title')}\n"
            f"{card.get('description', '')}\n"
            # The recalled memory reaches the CrewAI task itself, not only our own
            # prompt path - otherwise a crewai-backed run would silently be the one
            # run without its context.
            + (f"\n{card.get('memory_context')}" if card.get("memory_context") else "")
        )

    # -- backend 2: deterministic local runtime --------------------------
    def _run_local(
        self,
        crew_def: CrewDef,
        card: dict[str, Any],
        role_lookup: Optional[Callable[[str], Optional[AgentRole]]],
        started: float,
        *,
        scope: Optional[dict[str, Any]] = None,
        approved: bool = False,
    ) -> CrewRunResult:
        """Run the crew's pipeline for real - just without an LLM in the loop.

        Each step calls the tools its role is bound to, using the card's own
        tool arguments, and records what came back. This is a genuine execution
        of the workflow's mechanics (guardrails, tool calls, audit, traces); what
        it omits is the model's judgement about *which* tool to reach for next.
        """
        card_tools = {t.get("name"): t for t in (card.get("tools") or []) if isinstance(t, dict)}
        results: list[StepResult] = []
        findings: list[str] = []
        errors: list[str] = []
        total_tokens = 0
        total_cost = 0.0
        ran_any = False

        for step in crew_def.steps:
            step_started = time.monotonic()
            role = role_lookup(step.role) if role_lookup else None
            calls: list[dict[str, Any]] = []
            step_errors: list[str] = []

            wanted = [t for t in step.tools if t in card_tools] or [
                t for t in step.tools if not card_tools
            ]

            for tool_name in wanted:
                bound = card_tools.get(tool_name, {})
                tool_args = dict(bound.get("args") or {})
                try:
                    outcome = self.tool_executor(
                        tool_name,
                        tool_args,
                        scope=scope,
                        approved=approved,
                        card_id=card.get("card_id"),
                    )
                except Exception as exc:
                    step_errors.append(f"{tool_name}: {exc}")
                    calls.append({"tool": tool_name, "status": "error", "error": str(exc)})
                    continue
                status = outcome.get("status", "unknown")
                calls.append(
                    {
                        "tool": tool_name,
                        "status": status,
                        "dry_run": outcome.get("dry_run", True),
                        "duration_ms": outcome.get("duration_ms", 0),
                        "audit_hash": outcome.get("audit_hash"),
                        "command": outcome.get("command", ""),
                        "reasons": outcome.get("reasons") or [],
                    }
                )
                if status in ("ok", "dry_run"):
                    ran_any = True
                elif status == "denied":
                    step_errors.append(
                        f"{tool_name} refused by guardrails: {'; '.join(outcome.get('reasons') or [])}"
                    )
                elif status == "blocked":
                    step_errors.append(f"{tool_name} could not run: {outcome.get('stderr', '')[:200]}")

            if step.tools and not calls:
                step_errors.append(f"no bound tools for step '{step.role}' were applicable")

            summary = self._summarise_step(step.role, calls, errors=step_errors)
            results.append(
                StepResult(
                    role=step.role,
                    summary=summary,
                    tool_calls=calls,
                    duration_ms=int((time.monotonic() - step_started) * 1000),
                    status="error" if step_errors and not calls else "ok",
                )
            )
            errors.extend(step_errors)
            findings.extend(self._findings_from(calls))

        status = "ok" if ran_any and not errors else ("partial" if ran_any else "blocked")
        summary = self._summarise_crew(crew_def, card, results, findings)
        return CrewRunResult(
            crew=crew_def.name,
            backend="local",
            status=status,
            summary=summary,
            steps=results,
            findings=findings,
            tokens=total_tokens,
            cost_usd=total_cost,
            duration_ms=int((time.monotonic() - started) * 1000),
            errors=errors,
        )

    # -- narration -------------------------------------------------------
    @staticmethod
    def _summarise_step(role: str, calls: list[dict[str, Any]], *, errors: list[str]) -> str:
        if not calls:
            return f"{role}: no tool calls ({'; '.join(errors) if errors else 'nothing to do'})"
        parts = []
        for call in calls:
            mode = "dry-run" if call.get("dry_run") else "live"
            parts.append(f"{call['tool']} [{mode}] -> {call['status']} ({call.get('duration_ms', 0)}ms)")
        return f"{role}: " + "; ".join(parts)

    @staticmethod
    def _findings_from(calls: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        for call in calls:
            if call.get("status") == "denied":
                out.append(f"GUARDRAIL: {call['tool']} was refused ({'; '.join(call.get('reasons') or [])})")
            elif call.get("status") == "blocked":
                out.append(f"ENVIRONMENT: {call['tool']} unavailable on this host")
        return out

    @staticmethod
    def _summarise_crew(
        crew_def: CrewDef, card: dict[str, Any], steps: list[StepResult], findings: list[str]
    ) -> str:
        lines = [
            f"Crew '{crew_def.display_name}' completed {len(steps)} step(s) for card '{card.get('title')}'.",
        ]
        for step in steps:
            lines.append(f"- {step.summary}")
        if findings:
            lines.append("Findings needing attention:")
            lines.extend(f"  * {f}" for f in findings)
        else:
            lines.append("No guardrail or environment issues encountered.")
        return "\n".join(lines)
