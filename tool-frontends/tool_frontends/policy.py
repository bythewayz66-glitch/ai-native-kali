"""Live-execution policy: one source of truth for whether a tool may really run.

Blueprint ref: section 08 - stage 5 of the authorization chain ("live unlock").

The HTTP server, the MCP stdio transport and any future surface must all answer
the same question the same way: *has the operator enabled live execution for
this tool?* Three conditions have to hold together:

1. live execution is enabled globally (``TOOLS_LIVE=1``),
2. the tool's tier is within the operator's ceiling (``TOOLS_MAX_TIER``), and
3. the tool is named explicitly in the unlock list (``TOOLS_UNLOCK``).

Condition 3 is the important one. Enabling live execution globally is not
consent to run *any* tool live; the operator unlocks tools one at a time. Before
this module existed the policy was duplicated in ``server.py``, and duplicating
a safety check across two transports is how the two transports drift apart.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .spec import ToolSpec


@dataclass(frozen=True)
class LivePolicy:
    """Whether live tool execution is permitted, and for which tools."""

    live_enabled: bool = False
    max_tier: int = 3
    unlocked: frozenset[str] = field(default_factory=frozenset)

    # ------------------------------------------------------------- construct
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "LivePolicy":
        source = env if env is not None else os.environ
        raw_unlock = source.get("TOOLS_UNLOCK", "") or ""
        unlocked = frozenset(part.strip() for part in raw_unlock.split(",") if part.strip())
        try:
            max_tier = int(source.get("TOOLS_MAX_TIER", "3"))
        except (TypeError, ValueError):
            max_tier = 3
        return cls(
            live_enabled=source.get("TOOLS_LIVE", "0") == "1",
            max_tier=max(0, min(3, max_tier)),
            unlocked=unlocked,
        )

    # ---------------------------------------------------------------- policy
    def allows(self, spec: ToolSpec) -> bool:
        """May *spec* run live right now?"""
        if not self.live_enabled:
            return False
        if spec.tier > self.max_tier:
            return False
        return spec.name in self.unlocked

    def refusal(self, spec: ToolSpec) -> Optional[str]:
        """Human-readable reason live execution is refused, or ``None``."""
        if self.allows(spec):
            return None
        if not self.live_enabled:
            return "live execution is disabled for this host (TOOLS_LIVE is not 1)"
        if spec.tier > self.max_tier:
            return (
                f"'{spec.name}' is tier T{spec.tier}, above the operator ceiling "
                f"T{self.max_tier} (TOOLS_MAX_TIER)"
            )
        return f"'{spec.name}' is not in the operator unlock list (TOOLS_UNLOCK)"

    def describe(self) -> dict[str, Any]:
        return {
            "live_enabled": self.live_enabled,
            "max_tier": self.max_tier,
            "unlocked": sorted(self.unlocked),
        }
