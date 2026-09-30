"""Model-driven crew execution: the Phase 3 crew, with the card gate in the loop.

Blueprint ref: section 06 - "how crews map to security workflows" - plus the
Phase 3 requirement that a crew runs against a *local model* while the human
gate stays bound to the Kanban approval state.

Three rules shape this module, and each exists because of a specific way an LLM
in an offensive-security loop goes wrong:

1. **The model may only choose from a permitted set.** The candidate list is
   filtered by the role's tier ceiling *before* the model sees it, and the
   returned plan is validated *again* afterwards. A model that proposes
   ``sqlmap`` for a T1 recon role is refused with a recorded reason - it does not
   get to talk its way into a higher tier.

2. **Scope is enforced on the model's own arguments.** A plan that aims a tool at
   a host outside the authorized scope is refused before execution, not after.

3. **T2+ work passes a human gate.** The gate opens on the card and blocks; a
   rejection or timeout skips the tool and records why. Dry-run work is exempt
   because it touches nothing.

When no model is reachable the runner delegates to the deterministic executor, so
the card still moves and the tests still pass without a GPU.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from .crewai_adapter import CrewAIAdapter, CrewRunResult, StepResult
from .crews import CrewDef
from .gate import GATE_APPROVED, HumanFeedbackGate
from .model_client import LocalModelClient
from .roles import AgentRole

log = logging.getLogger("agent_runtime.llm_crew")


class PlanRefusal(Exception):
    """A proposed plan step was refused before execution."""


def _step_role(crew: CrewDef, role_name: str) -> Optional[str]:
    for step in crew.steps:
        if step.role == role_name:
            return step.role
    return None


class LLMCrewRunner:
    """Runs a crew with a model choosing the calls, under hard constraints.

    :param adapter: the deterministic executor used for the no-model fallback.
    :param model: the local-model client.
    :param gate: the human-feedback gate bound to card approvals.
    :param tier_lookup: ``tool_name -> tier``, used to enforce role ceilings and
        decide which calls need a gate. Defaults to the card's own tool bindings.
    """

    def __init__(
        self,
        adapter: CrewAIAdapter,
        *,
        model: Optional[LocalModelClient] = None,
        gate: Optional[HumanFeedbackGate] = None,
        tier_lookup: Optional[Callable[[str], Optional[int]]] = None,
        gate_timeout_s: Optional[float] = None,
    ) -> None:
        self.adapter = adapter
        self.model = model
        self.gate = gate
        self.tier_lookup = tier_lookup
        self.gate_timeout_s = gate_timeout_s
        self.runs = 0
        self.model_runs = 0
        self.fallback_runs = 0
        self.refused_steps = 0
        self.gated_steps = 0
        self.gate_denials = 0
        #: Steps executed through the real CrewAI backend, and steps that fell
        #: back to the deterministic executor because crewai was absent or the
        #: crewai call raised. Reported so a run can say which path it took.
        self.crewai_steps = 0
        self.crewai_fallbacks = 0
        self.tokens = 0
        self.cost_usd = 0.0
        self.last_plan_error: Optional[str] = None

    # ------------------------------------------------------------- helpers
    def _tier_of(self, tool: str, card_tools: dict[str, dict[str, Any]]) -> int:
        if tool in card_tools and isinstance(card_tools[tool].get("tier"), int):
            return int(card_tools[tool]["tier"])
        if self.tier_lookup is not None:
            found = self.tier_lookup(tool)
            if found is not None:
                return int(found)
        # Unknown tier: treat as the most cautious value so it cannot slip past a
        # gate or a role ceiling.
        return 3

    def _candidates(
        self,
        crew: CrewDef,
        card_tools: dict[str, dict[str, Any]],
        role_lookup: Callable[[str], Optional[AgentRole]],
        spec_lookup: Callable[[str], Optional[Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Tools the model is allowed to see, and what was withheld and why."""
        allowed: list[dict[str, Any]] = []
        withheld: list[str] = []
        seen: set[tuple[str, str]] = set()
        for step in crew.steps:
            role = role_lookup(step.role)
            for tool in step.tools:
                key = (step.role, tool)
                if key in seen:
                    continue
                seen.add(key)
                tier = self._tier_of(tool, card_tools)
                if role is not None and not role.may_use(tool, tier):
                    withheld.append(
                        f"{tool} (T{tier}) withheld from role '{step.role}' "
                        f"(ceiling T{role.max_tier}, tools {role.tools})"
                    )
                    continue
                if not card_tools and spec_lookup is None:
                    # No bindings and no registry: nothing is provably permitted.
                    withheld.append(f"{tool} withheld: no card binding to confirm its arguments")
                    continue
                spec = spec_lookup(tool) if spec_lookup else None
                allowed.append(
                    {
                        "name": tool,
                        "tier": tier,
                        # Carried through so the step validator can enforce scope on the
                        # *flag* rather than on the tier - see _validate_step. The tier
                        # rule is OR'd in as the fallback for when no spec could be
                        # resolved at this layer: without that, an unresolvable spec
                        # would read as "needs no scope" and silently disable the check.
                        "requires_scope": bool(getattr(spec, "requires_scope", False)) or tier >= 1,
                        "role": step.role,
                        "description": (getattr(spec, "description", "") or step.description or "")[:220],
                        "binary": getattr(spec, "binary", "?"),
                        "args": dict(card_tools.get(tool, {}).get("args") or {}),
                    }
                )
        return allowed, withheld

    def _validate_step(
        self,
        proposal: dict[str, Any],
        crew: CrewDef,
        allowed: list[dict[str, Any]],
        scope: Optional[dict[str, Any]],
        role_lookup: Callable[[str], Optional[AgentRole]],
        card_tools: dict[str, dict[str, Any]],
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Is this proposed call permitted? Returns (candidate, refusal)."""
        tool = proposal.get("tool")
        role_name = proposal.get("role")
        if not isinstance(tool, str) or not tool:
            return None, "plan step has no tool name"
        match = next((c for c in allowed if c["name"] == tool), None)
        if match is None:
            # Not in the permitted set. Say precisely *why*, because "not permitted"
            # is useless to an operator reviewing a refused plan step.
            for crew_step in crew.steps:
                if tool in crew_step.tools:
                    step_tier = self._tier_of(tool, card_tools)
                    step_role = role_lookup(crew_step.role)
                    if step_role is not None and not step_role.may_use(tool, step_tier):
                        return None, (
                            f"role '{crew_step.role}' may not use '{tool}' at T{step_tier} "
                            f"(ceiling T{step_role.max_tier})"
                        )
                    return None, (
                        f"tool '{tool}' was withheld: the card carries no binding to confirm its arguments"
                    )
            return None, f"tool '{tool}' is not in the permitted set for this card"
        if isinstance(role_name, str) and role_name and _step_role(crew, role_name) is None:
            return None, f"role '{role_name}' is not part of the '{crew.name}' crew"
        effective_role = role_name if isinstance(role_name, str) and role_name else match["role"]
        role = role_lookup(effective_role)
        tier = self._tier_of(tool, card_tools)
        if role is not None and not role.may_use(tool, tier):
            return None, (
                f"role '{effective_role}' may not use '{tool}' at T{tier} "
                f"(ceiling T{role.max_tier})"
            )
        # Scope check on whatever target the model proposed.
        args = proposal.get("args")
        args = dict(args) if isinstance(args, dict) else {}
        target = args.get("target") or args.get("host") or args.get("domain") or args.get("url")
        # Scope *coverage* is gated on the tool's declared ``requires_scope`` OR its
        # tier. Gating on ``tier >= 1`` alone was a real escape in a third layer: this
        # validator consulted the scope only from T1 up, so a plan could aim a
        # scope-requiring *T0* tool (e.g. dns_query, tls_probe) at any host in the
        # world and the step validated cleanly - the bridge and the guardrail engine
        # would both refuse it, but the board would already have shown it as planned
        # work. Tier governs whether a scope is *mandated* (T2+); the flag governs
        # whether one is *enforced* once attached. Taking the union means the check
        # holds whichever signal is available, and cannot be silently switched off by
        # a spec that fails to resolve. All three layers now agree.
        requires_scope = bool(match.get("requires_scope"))
        if target and requires_scope:
            from kanban_core.models import Scope

            if not scope or not (scope.get("targets") or scope.get("cidrs")):
                return None, f"'{tool}' on target '{target}' refused: card carries no authorization scope"
            scope_obj = Scope.model_validate(scope)
            if scope_obj.is_expired():
                return None, f"'{tool}' refused: the card's authorization scope has expired"
            if not scope_obj.covers(str(target)):
                return None, (
                    f"target '{target}' is outside the authorized scope ({scope_obj.summary()})"
                )
        return {**match, "role": effective_role, "args": args or match.get("args") or {}}, None

    # ----------------------------------------------------------------- run
    def run(
        self,
        crew: CrewDef,
        card: dict[str, Any],
        *,
        role_lookup: Optional[Callable[[str], Optional[AgentRole]]] = None,
        scope: Optional[dict[str, Any]] = None,
        approved: bool = False,
        spec_lookup: Optional[Callable[[str], Optional[Any]]] = None,
    ) -> CrewRunResult:
        """Plan with the model, validate, gate, execute, and record."""
        from .roles import get_role

        lookup = role_lookup or get_role
        started = time.monotonic()
        self.runs += 1

        model = self.model
        if model is None or not model.config.enabled:
            return self._fallback(crew, card, lookup, started, scope, approved, "no model configured")

        probe = model.probe()
        if not probe.get("available"):
            return self._fallback(
                crew, card, lookup, started, scope, approved,
                probe.get("error") or "model endpoint not reachable",
            )

        card_tools = {t.get("name"): t for t in (card.get("tools") or []) if isinstance(t, dict)}
        allowed, withheld = self._candidates(crew, card_tools, lookup, spec_lookup or (lambda _n: None))

        response = model.plan(crew=crew.name, card=card, candidates=allowed)
        self.tokens += response.total_tokens
        self.cost_usd = round(self.cost_usd + response.cost_usd, 6)
        if not response.ok:
            self.last_plan_error = response.error
            return self._fallback(
                crew, card, lookup, started, scope, approved, f"model call failed: {response.error}"
            )

        plan = response.parsed or {}
        proposals = plan.get("steps")
        if not isinstance(proposals, list):
            self.last_plan_error = "model returned no 'steps' array"
            return self._fallback(
                crew, card, lookup, started, scope, approved, self.last_plan_error
            )

        self.model_runs += 1
        results: list[StepResult] = []
        findings: list[str] = [f"WITHHELD: {w}" for w in withheld]
        errors: list[str] = []
        refusals: list[str] = []
        calls_by_role: dict[str, list[dict[str, Any]]] = {step.role: [] for step in crew.steps}
        gate_notes: list[str] = []
        ran_any = False

        for proposal in proposals:
            if not isinstance(proposal, dict):
                refusals.append("plan contained a non-object step")
                continue
            candidate, refusal = self._validate_step(proposal, crew, allowed, scope, lookup, card_tools)
            if refusal:
                self.refused_steps += 1
                refusals.append(refusal)
                findings.append(f"REFUSED: {refusal}")
                continue
            assert candidate is not None
            tool_name = candidate["name"]
            tier = int(candidate["tier"])
            role_name = candidate["role"]
            tool_args = dict(candidate.get("args") or {})
            step_approved = approved

            # -- human gate on intrusive work -------------------------------
            if tier >= 2 and self.gate is not None:
                self.gated_steps += 1
                decision = self.gate.request(
                    card["card_id"],
                    reason=(
                        f"agent '{role_name}' wants to run '{tool_name}' (T{tier}) "
                        f"on {tool_args.get('target') or 'the card target'}"
                    ),
                    requested_by=role_name,
                    tool=tool_name,
                    tier=tier,
                    timeout_s=self.gate_timeout_s,
                    dry_run=False,
                )
                gate_notes.append(
                    f"gate[{tool_name}] -> {decision.outcome}"
                    + (f" by {decision.decided_by}" if decision.decided_by else "")
                )
                if decision.outcome != GATE_APPROVED or not decision.approved:
                    self.gate_denials += 1
                    refusal = (
                        f"'{tool_name}' blocked by the human gate ({decision.outcome}"
                        f"{': ' + decision.note if decision.note else ''})"
                    )
                    findings.append(f"GATED: {refusal}")
                    errors.append(refusal)
                    continue
                step_approved = True

            # -- execute ----------------------------------------------------
            # The step has already passed the permitted-set, role-ceiling, scope
            # and human-gate checks above. Execution goes through CrewAI when it
            # is installed, and through the deterministic executor otherwise -
            # the *validated* tool name and arguments are what is handed over, so
            # the backend that runs the call cannot widen what was allowed.
            call_started = time.monotonic()
            try:
                outcome = self._execute_step(
                    crew, card, role_name, tool_name, tool_args, scope, step_approved
                )
            except Exception as exc:
                errors.append(f"{tool_name}: {exc}")
                calls_by_role.setdefault(role_name, []).append(
                    {"tool": tool_name, "status": "error", "error": str(exc)}
                )
                continue

            status = outcome.get("status", "unknown")
            calls_by_role.setdefault(role_name, []).append(
                {
                    "tool": tool_name,
                    "status": status,
                    "dry_run": outcome.get("dry_run", True),
                    "duration_ms": outcome.get("duration_ms", 0),
                    "audit_hash": outcome.get("audit_hash"),
                    "command": outcome.get("command", ""),
                    "reasons": outcome.get("reasons") or [],
                    "rationale": proposal.get("rationale", ""),
                    "planned_by": "model",
                }
            )
            if status in ("ok", "dry_run"):
                ran_any = True
            elif status == "denied":
                errors.append(
                    f"{tool_name} refused by guardrails: {'; '.join(outcome.get('reasons') or [])}"
                )
                findings.append(f"GUARDRAIL: {tool_name} refused by the tool layer")
            elif status == "blocked":
                errors.append(f"{tool_name} could not run: {str(outcome.get('stderr', ''))[:200]}")
            _ = int((time.monotonic() - call_started) * 1000)

        # -- one StepResult per crew role, in crew order --------------------
        for step in crew.steps:
            calls = calls_by_role.get(step.role, [])
            summaries = [self._summarise_call(c) for c in calls]
            rationale = next((c.get("rationale") for c in calls if c.get("rationale")), "")
            summary = f"{step.role}: " + ("; ".join(summaries) if summaries else "no tool calls")
            if rationale:
                summary += f"  [model rationale: {rationale[:160]}]"
            results.append(StepResult(role=step.role, summary=summary, tool_calls=calls, status="ok"))

        status = "ok" if ran_any and not errors else ("partial" if ran_any else "blocked")
        summary_lines = [
            f"Crew '{crew.display_name}' ran on model '{response.model}' for card '{card.get('title')}'.",
            f"Plan: {len(proposals)} proposed, {len(refusals)} refused, {len(gate_notes)} gated.",
        ]
        if plan.get("summary"):
            summary_lines.append(f"Model summary: {plan['summary']}")
        summary_lines.extend(gate_notes)
        if errors:
            summary_lines.append("Issues:")
            summary_lines.extend(f"  * {e}" for e in errors)

        return CrewRunResult(
            crew=crew.name,
            backend="local-model",
            status=status,
            summary="\n".join(summary_lines),
            steps=results,
            findings=findings,
            tokens=response.total_tokens,
            cost_usd=response.cost_usd,
            duration_ms=int((time.monotonic() - started) * 1000),
            errors=errors,
        )

    # ------------------------------------------------------------ execution
    def _execute_step(
        self,
        crew: CrewDef,
        card: dict[str, Any],
        role_name: str,
        tool_name: str,
        tool_args: dict[str, Any],
        scope: Optional[dict[str, Any]],
        approved: bool,
    ) -> dict[str, Any]:
        """Run one validated step, through CrewAI when it is available.

        The plan has already been validated; this only decides *how* the call is
        made. When crewai is importable the step is handed to a crewai agent via
        :meth:`CrewAIAdapter.run_plan`, which calls back into the same guardrailed
        executor. When it is not - or when the crewai call raises - the
        deterministic executor runs the identical call, so the card still moves
        and the audit trail is identical either way.
        """
        adapter = self.adapter
        if getattr(adapter, "crewai_available", False):
            try:
                result = adapter.run_plan(
                    crew,
                    card,
                    [
                        {
                            "role": role_name,
                            "tool": tool_name,
                            "args": tool_args,
                            "approved": approved,
                        }
                    ],
                    role_lookup=self._role_lookup,
                    scope=scope,
                    approved=approved,
                )
                # run_plan records the calls it made; take the one for this tool.
                for step in result.steps:
                    for call in step.tool_calls:
                        if call.get("tool") == tool_name:
                            self.crewai_steps += 1
                            return {
                                "tool": tool_name,
                                "status": call.get("status", "unknown"),
                                "dry_run": call.get("dry_run", True),
                                "duration_ms": call.get("duration_ms", 0),
                                "audit_hash": call.get("audit_hash"),
                                "command": call.get("command", ""),
                                "reasons": call.get("reasons") or [],
                            }
                # crewai ran but recorded no call for this tool: fall through to
                # the deterministic executor rather than reporting a phantom.
                self.crewai_fallbacks += 1
            except Exception:
                self.crewai_fallbacks += 1
        return adapter.tool_executor(
            tool_name,
            tool_args,
            scope=scope,
            approved=approved,
            card_id=card.get("card_id"),
        )

    @staticmethod
    def _role_lookup(name: str) -> Optional[AgentRole]:
        from .roles import get_role

        return get_role(name)

    # ------------------------------------------------------------- fallback
    def _fallback(
        self,
        crew: CrewDef,
        card: dict[str, Any],
        lookup: Callable[[str], Optional[AgentRole]],
        started: float,
        scope: Optional[dict[str, Any]],
        approved: bool,
        reason: str,
    ) -> CrewRunResult:
        """No model: run the deterministic pipeline and say so plainly."""
        self.fallback_runs += 1
        result = self.adapter.run(
            crew, card, role_lookup=lookup, scope=scope, approved=approved
        )
        result.summary = (
            f"[deterministic fallback: {reason}]\n" + result.summary
        )
        result.errors = list(result.errors) + [f"model unavailable: {reason}"]
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.backend = "local (no model)"
        return result

    @staticmethod
    def _summarise_call(call: dict[str, Any]) -> str:
        mode = "dry-run" if call.get("dry_run") else "live"
        return f"{call['tool']} [{mode}] -> {call.get('status')} ({call.get('duration_ms', 0)}ms)"

    def status(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "model_runs": self.model_runs,
            "fallback_runs": self.fallback_runs,
            "refused_steps": self.refused_steps,
            "gated_steps": self.gated_steps,
            "gate_denials": self.gate_denials,
            "crewai_steps": self.crewai_steps,
            "crewai_fallbacks": self.crewai_fallbacks,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "last_plan_error": self.last_plan_error,
        }
