"""Agent runtime: the CrewAI bridge and the crew definitions.

Blueprint ref: sections 03.2 (Kanban <-> CrewAI bridge) and 06 (CrewAI
integration). The bridge is the only component that writes card results, and it
is deliberately the *only* place where an agent touches the board.
"""
from .bridge import Bridge, BridgeStats
from .client import KanbanClient, KanbanError
from .crews import CREWS, CrewDef, get_crew
from .roles import ROLE_REGISTRY, AgentRole, get_role

__version__ = "0.1.0"

__all__ = [
    "Bridge",
    "BridgeStats",
    "CREWS",
    "CrewDef",
    "KanbanClient",
    "KanbanError",
    "ROLE_REGISTRY",
    "AgentRole",
    "get_crew",
    "get_role",
]
