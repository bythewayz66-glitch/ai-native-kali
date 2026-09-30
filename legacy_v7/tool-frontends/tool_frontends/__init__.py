"""Tool frontend layer: typed wrappers over Kali binaries.

Blueprint ref: section 05 (AI frontends for Kali tools) and section 08
(guardrails). Every wrapper is described by a :class:`ToolSpec`, validated by
the guardrail engine, executed under a sandbox, and written to a hash-chained
audit log.

    intent -> tool spec -> guardrail decision -> sandbox -> audit -> trace
"""
from .audit import ToolAuditLog
from .guardrails import GuardrailDecision, evaluate
from .registry import REGISTRY, ToolRegistry, get_registry
from .runner import SandboxLimits, ToolResult, run_tool
from .spec import TOOL_TIERS, ToolSpec

__version__ = "0.1.0"

__all__ = [
    "GuardrailDecision",
    "REGISTRY",
    "SandboxLimits",
    "TOOL_TIERS",
    "ToolAuditLog",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "evaluate",
    "get_registry",
    "run_tool",
]
