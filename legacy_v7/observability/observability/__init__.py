"""Observability service for the AI-native Kali stack.

Blueprint ref: section 04 (native AI observability panels). This service ingests
the two event streams the OS already emits - Kanban card events and tool audit
records - and projects them into the panels the shell renders:

* agent activity timeline  (04.1)
* token / cost / latency   (04.2)
* tool-call traces         (04.3)
* model health             (04.4)
* prompt / response        (04.5)
* hash-chained audit log   (04.6)
* alerts                   (04.7)
"""
from .collector import Collector
from .metrics import Metrics

__version__ = "0.1.0"

__all__ = ["Collector", "Metrics"]
