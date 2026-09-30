"""First-boot setup wizard surface (Phase 7, item 6).

This is deliberately a *surface*, not an installer. The Phase 4 delivery plan's own
warning applies and is worth repeating: **a wizard that configures a system the ISO
does not ship is worse than no wizard** - it produces a machine that believes it is
configured while the underlying capability was never built. So the scope here is:

* the step model (what to ask, in what order, what each step needs first);
* the answers it *would* write, as a plain environment mapping;
* honest per-step readiness against what is actually present on the host.

What it does **not** do is apply those answers to a live system. The apply path is
gated behind ``apply()``, which refuses unless called with an explicit confirmation
flag, and the refusal is asserted in the tests rather than assumed - a wizard that
silently no-ops is exactly the failure this docstring warns about.

Written as a pure module (no GTK, no network) so the step model is testable on a
headless host, in keeping with the shell's own design.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Optional

#: Answer keys, with the environment variable each ultimately feeds. Keeping the
#: mapping here - rather than having each caller invent one - is what makes the
#: wizard's output diffable against the service configuration.
ENV_KEYS = {
    "engagement": "HERMES_ENGAGEMENT",
    "scope": "HERMES_SCOPE",
    "model_url": "MODEL_BASE_URL",
    "model_name": "MODEL_NAME",
    "embedder": "MEMORY_EMBEDDER",
    "tools_live": "TOOLS_LIVE",
    "telemetry": "HERMES_TELEMETRY",
}


@dataclass
class Step:
    """One question the first boot asks."""

    key: str
    title: str
    prompt: str
    #: Key whose answer must be present before this step is worth asking.
    #: ``None`` for the opening steps.
    requires: Optional[str] = None
    #: Extra context the user needs to answer well.
    help: str = ""
    default: str = ""
    #: A step that cannot be satisfied on this host should not be shown as one
    #: that can.
    optional: bool = False


#: The step order. Authorisation comes first on purpose: scope is the one answer
#: that changes what every later step is *allowed* to do, and asking for
#: telemetry preferences before establishing the engagement would invert that.
STEPS: tuple[Step, ...] = (
    Step(
        key="engagement",
        title="Engagement",
        prompt="Name this engagement.",
        help="Every memory, card and audit row is scoped to it.",
        default="local-lab",
    ),
    Step(
        key="scope",
        title="Authorised scope",
        prompt="Which hosts or networks are authorised?",
        requires="engagement",
        help="Comma-separated. Cards without scope cannot leave T0.",
    ),
    Step(
        key="model_url",
        title="Model endpoint",
        prompt="Where is the local model?",
        requires="engagement",
        help="An Ollama-compatible base URL. Leave blank for deterministic mode.",
        default="http://127.0.0.1:11434",
        optional=True,
    ),
    Step(
        key="model_name",
        title="Model",
        prompt="Which model should crews plan with?",
        requires="model_url",
        default="llama3.1",
        optional=True,
    ),
    Step(
        key="embedder",
        title="Memory embedder",
        prompt="Which embedding backend?",
        requires="engagement",
        help="auto, hashing or ollama. auto uses the model endpoint when reachable.",
        default="auto",
    ),
    Step(
        key="tools_live",
        title="Live tool execution",
        prompt="Allow tools to run against real targets?",
        requires="scope",
        help="Off means dry-run only. Leaving this off is the safe default.",
        default="0",
    ),
    Step(
        key="telemetry",
        title="Telemetry",
        prompt="Record local run telemetry?",
        requires="engagement",
        default="1",
    ),
)

STEP_BY_KEY = {step.key: step for step in STEPS}


@dataclass
class Readiness:
    """What the host can actually do, so the wizard does not promise more."""

    model_reachable: bool = False
    embedder_available: bool = False
    live_tools_available: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_reachable": self.model_reachable,
            "embedder_available": self.embedder_available,
            "live_tools_available": self.live_tools_available,
            "notes": list(self.notes),
        }


def probe_readiness(
    *,
    env: Optional[dict[str, str]] = None,
    which: Any = None,
    prober: Any = None,
) -> Readiness:
    """Check what is genuinely present, without failing when it is not.

    ``prober`` is an optional zero-argument callable returning a bool. It exists so
    the readiness check is testable *without* a model endpoint - the same reason
    the embedder's transport is injectable. A probe that cannot be tested offline
    gets tested by hand once and then rots.
    """
    env = env if env is not None else dict(os.environ)
    which = which or shutil.which
    readiness = Readiness()
    if prober is not None:
        try:
            readiness.model_reachable = bool(prober())
        except Exception as exc:  # noqa: BLE001 - a broken probe is "not reachable"
            readiness.model_reachable = False
            readiness.notes.append(f"model probe failed: {exc}")
    readiness.embedder_available = readiness.model_reachable or bool(
        env.get("MEMORY_EMBED_URL")
    )
    readiness.live_tools_available = bool(which("nmap"))
    if not readiness.model_reachable:
        readiness.notes.append("no model endpoint reachable: crews will use the deterministic runner")
    if not readiness.live_tools_available:
        readiness.notes.append("nmap not on PATH: tools stay in dry-run")
    return readiness


class SetupWizard:
    """Collects first-boot answers. Does not touch the system until told to."""

    def __init__(self, *, readiness: Optional[Readiness] = None) -> None:
        self.readiness = readiness or Readiness()
        self.answers: dict[str, str] = {}

    # ------------------------------------------------------------- flow
    def steps(self) -> list[Step]:
        """Every step, in order. Static: the plan does not depend on answers."""
        return list(STEPS)

    def pending(self) -> list[Step]:
        """The steps still worth asking, given what has been answered.

        A step whose ``requires`` is unanswered is skipped rather than shown and
        rejected: asking for a model name before knowing whether there is a model
        endpoint is how a wizard collects an answer it will then ignore.

        An optional step whose prerequisite answered "none"/blank is also skipped,
        which is what keeps "no model" from producing a phantom model name.
        """
        out: list[Step] = []
        for step in self.steps():
            if step.key in self.answers:
                continue
            if step.requires is not None:
                parent = self.answers.get(step.requires)
                if parent is None or parent.strip() == "":
                    continue
            out.append(step)
        return out

    def next_step(self) -> Optional[Step]:
        remaining = self.pending()
        return remaining[0] if remaining else None

    def answer(self, key: str, value: str) -> dict[str, Any]:
        """Record an answer. Unknown keys are refused, not stored."""
        if key not in STEP_BY_KEY:
            raise KeyError(f"unknown setup step {key!r}")
        self.answers[key] = str(value).strip()
        return self.state()

    def answer_defaults(self) -> dict[str, Any]:
        """Take the default for every currently-pending step.

        This is the "just press through it" path. It deliberately does **not**
        invent a value for a step with no default and no prerequisite answer: the
        scope step in particular must be answered by a person.

        Iterates to a fixpoint rather than taking one pass, because answering a
        step can *unlock* a later default: filling ``model_url`` makes
        ``model_name`` askable. A single pass left that second step pending and the
        wizard looking stuck, which is what the test
        ``test_defaults_reach_steps_they_unlock`` pins.
        """
        while True:
            filled = False
            for step in list(self.pending()):
                if step.default:
                    self.answers[step.key] = step.default
                    filled = True
            if not filled:
                break
        return self.state()

    # ------------------------------------------------------------ output
    def env(self) -> dict[str, str]:
        """The answers as environment variables, for the services to consume."""
        out: dict[str, str] = {}
        for key, env_name in ENV_KEYS.items():
            value = self.answers.get(key)
            if value:
                out[env_name] = value
        return out

    def state(self) -> dict[str, Any]:
        return {
            "answers": dict(self.answers),
            "pending": [s.key for s in self.pending()],
            "complete": not self.pending(),
            "env": self.env(),
            "readiness": self.readiness.as_dict(),
        }

    def warnings(self) -> list[str]:
        """Things the user should know before finishing, derived from readiness.

        These are warnings, not errors: every one of them describes a system that
        still works, in a reduced mode. Refusing to finish setup because no model
        endpoint exists would leave the machine unusable over an optional feature.
        """
        out: list[str] = []
        if self.answers.get("tools_live") == "1" and not self.answers.get("scope"):
            out.append("live tools requested but no scope set: every tool call will be refused")
        if self.answers.get("model_url") and not self.readiness.model_reachable:
            out.append("a model endpoint was configured but is not reachable: crews will fall back")
        if self.answers.get("embedder") == "ollama" and not self.readiness.embedder_available:
            out.append("embedder=ollama but no endpoint is reachable: recall will degrade to hashing")
        return out

    # ------------------------------------------------------------- apply
    def apply(self, *, confirmed: bool = False) -> dict[str, Any]:
        """Refuse to write unless explicitly confirmed.

        The refusal is the *point*: a wizard that returns success while changing
        nothing is the failure mode the module docstring warns about, so this
        raises rather than quietly returning ``{}``.
        """
        if not confirmed:
            raise PermissionError(
                "setup.apply requires confirmed=True: first-boot writes are not implicit"
            )
        return {"applied": self.env(), "steps": len(self.answers)}
