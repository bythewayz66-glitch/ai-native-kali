"""Tool specifications: the typed contract for every Kali frontend.

Blueprint ref: section 05 - each frontend has a tool name, category, guardrail
tier, and an *integration approach* (CLI wrapper). The command template is
explicitly separated into a ``dry_run`` form (always safe, no packets) and a
``live`` form (only reachable when the operator has unlocked it).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Category = Literal[
    "information-gathering",
    "vulnerability-analysis",
    "web-application",
    "password-attacks",
    "wireless",
    "forensics",
    "reverse-engineering",
    "exploitation",
    "reporting",
    "system",
    "cloud",
    "social-engineering",
]

#: Guardrail tier -> meaning (mirrors blueprint section 05).
TOOL_TIERS: dict[int, str] = {
    0: "T0 passive - read-only lookup, no packets reach the target",
    1: "T1 low-impact - active but non-destructive probing",
    2: "T2 intrusive - exploitation or auth attempts; needs scope + human approval",
    3: "T3 high-impact - exploit execution / wireless attacks; needs scope + approval + sandbox",
}


class ParamSpec(BaseModel):
    """One typed parameter of a tool."""

    name: str
    type: Literal["string", "integer", "boolean", "enum"] = "string"
    required: bool = False
    default: Any = None
    choices: Optional[list[str]] = None
    max_length: int = 256
    description: str = ""


class ToolSpec(BaseModel):
    """Everything the runtime needs to describe, guard and invoke a tool."""

    name: str
    binary: str
    category: Category
    tier: int = 0
    description: str = ""
    intent_examples: list[str] = Field(default_factory=list)
    integration: Literal["cli_wrapper", "gui_panel", "agent_skill"] = "cli_wrapper"

    params: list[ParamSpec] = Field(default_factory=list)
    #: parameter names that carry the target host (used for scope enforcement)
    target_params: list[str] = Field(default_factory=lambda: ["target"])

    #: Address-shaped parameters that are deliberately **not** independent
    #: targets, so scope coverage does not apply to them.
    #:
    #: This exists so no parameter can be silently exempt. The value classifier
    #: in ``targets.py`` flags every network-identifier argument, and the
    #: authoring lint refuses any flagged parameter that is neither declared nor
    #: listed here. An exemption therefore has to be written down, explained in a
    #: comment, and shows up in a grep - which is the difference between a
    #: considered decision and a blind spot.
    scope_skip_params: list[str] = Field(default_factory=list)

    #: command template (dry-run): never sends traffic to the target
    dry_run_template: str = ""
    #: command template (live): the real invocation
    live_template: str = ""

    #: guardrail flags
    requires_scope: bool = False
    requires_approval: bool = False
    requires_sandbox: bool = False

    #: Declared local footprint (see ``effects.py``). Empty means "not declared".
    #: Only a *declared* footprint is enforced - inference is report-only, so a
    #: heuristic can never deny a tool whose real footprint is benign.
    effects: list[str] = Field(default_factory=list)

    @property
    def effects_declared(self) -> bool:
        """True when the author stated the footprint rather than it being inferred."""
        return bool(self.effects)

    def inferred_effects(self) -> list[str]:
        """Best-effort footprint from binary/tier/templates when none is declared.

        Report-only. It exists to size the migration below, not to gate a call:
        keyword inference cannot tell ``kubectl -o json`` (an output *format*) from
        ``-o file`` (a write), and a heuristic that denies a read-only tool is a
        heuristic that gets deleted rather than fixed.
        """
        from .effects import infer_effects

        return infer_effects(
            binary=self.binary,
            tier=self.tier,
            requires_scope=self.requires_scope,
            live_template=self.live_template,
            dry_run_template=self.dry_run_template,
        )

    def enforced_effects(self) -> list[str]:
        """The footprint the guardrails actually gate on: the declaration.

        Deliberately **declared-only**. Enforcement runs on facts an author
        stated, so the gate is deterministic and cannot refuse a benign tool
        because a substring matched its template. Where nothing is declared the
        returned list is empty and :attr:`effects_declared` is False, which the
        registry reports as a migration backlog - the gap is measured rather
        than assumed away, and each declaration shrinks it.
        """
        from .effects import normalize

        return normalize(self.effects)

    def effect_report(self) -> dict[str, Any]:
        """Machine-readable footprint: the enforced list, the inferred list, the
        derived flags, and which of the two the gate actually read."""
        from .effects import describe

        report = describe(self.enforced_effects(), declared=self.effects_declared)
        report["inferred"] = self.inferred_effects()
        report["enforced"] = report["declared"]
        return report

    #: how to explain results back to the human (blueprint 05: result explanation)
    explain: str = ""
    #: suggested next steps after a run
    next_steps: list[str] = Field(default_factory=list)
    timeout_s: int = 60

    @property
    def scope_required(self) -> bool:
        """Must this tool's target be checked against the authorization scope?

        Tier alone is **not** the answer. ``requires_scope`` is the declaration
        that the tool reaches a network target; the registry already refuses to
        register a T2+ tool without it, so flag and tier agree wherever the tier
        says they must. Reading the flag keeps the check honest for the T0/T1
        tools that declare it deliberately - see ``guardrails.scope_is_required``
        for the fail-closed floor the callers add on top.
        """
        return self.requires_scope

    def target_value(self, args: dict[str, Any]) -> Optional[str]:
        for key in self.target_params:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def defaults(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params if p.default is not None}

    def validate_args(self, args: dict[str, Any]) -> list[str]:
        """Return a list of human-readable argument problems (empty = ok)."""
        problems: list[str] = []
        known = {p.name for p in self.params}
        for key in args:
            if key.startswith("__"):
                continue  # internal bookkeeping (tokens/cost)
            if key not in known:
                problems.append(f"unknown parameter '{key}'")
        for p in self.params:
            value = args.get(p.name, p.default)
            if p.required and (value is None or value == ""):
                problems.append(f"missing required parameter '{p.name}'")
                continue
            if value is None:
                continue
            if p.type == "integer" and not isinstance(value, int):
                problems.append(f"parameter '{p.name}' must be an integer")
            if p.type == "boolean" and not isinstance(value, bool):
                problems.append(f"parameter '{p.name}' must be a boolean")
            if p.type == "string" and not isinstance(value, str):
                problems.append(f"parameter '{p.name}' must be a string")
            if p.type == "string" and isinstance(value, str) and len(value) > p.max_length:
                problems.append(f"parameter '{p.name}' exceeds {p.max_length} characters")
            if p.type == "enum" and p.choices and value not in p.choices:
                problems.append(f"parameter '{p.name}' must be one of {p.choices}")
        return problems

    def render(self, template: str, args: dict[str, Any]) -> str:
        """Fill a command template. Values are sanitised, never shell-expanded."""
        merged = dict(self.defaults())
        merged.update({k: v for k, v in args.items() if not k.startswith("__")})
        safe: dict[str, str] = {}
        for key, value in merged.items():
            if value is None:
                continue
            if isinstance(value, bool):
                safe[key] = "true" if value else "false"
            else:
                safe[key] = sanitize(str(value))
        try:
            return template.format(**safe)
        except KeyError as exc:  # a required value was absent
            raise ValueError(f"cannot render command: missing {exc}") from exc


_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_:/@,+*=[]")


def sanitize(value: str) -> str:
    """Reduce a substituted *value* to a single safe token.

    Two layers of defence live here:

    1. an allowlist - anything outside it (``;``, ``$``, backticks, quotes,
       newlines, ``&``, ``|``) is dropped; and
    2. **whitespace is removed**, so a value can never expand into additional
       arguments. Without this, ``--target \"evil.com -oN /etc/passwd\"`` would
       smuggle a second flag into the argv. Templates supply their own spacing
       between arguments, so removing spaces from values costs nothing.

    The runner additionally never uses a shell, making this defence in depth
    rather than the only barrier.
    """
    cleaned = "".join(ch for ch in value if ch in _ALLOWED)
    return cleaned.replace("..", ".").strip()
