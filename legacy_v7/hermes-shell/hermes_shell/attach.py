"""File-manager drag-and-drop onto Kanban cards.

Item 3 of the Phase 4 kickoff: "make the file manager draggable onto Kanban cards
so a file can be attached to a card".

Why the payload construction lives here and not in the route
-----------------------------------------------------------
The interesting part of a drop is not the HTTP call - it is deciding *what* gets
attached. That decision (which artifact kind a file is, whether the entry is
usable at all, what the summary should say) is pure, so it is kept out of the
request handler where it can be tested directly and reused by any surface that
wants to attach a file: the panel, a future GTK shell, or a CLI.

Honesty about what is attached
------------------------------
This attaches a **reference**, not the bytes. There is no blob store in the
scaffold, so an artifact records the file-manager path, its size and - when the
caller supplies the content - its sha256. Recording a path and calling it a
stored file would be a lie the audit trail would later repeat, so the payload
carries ``path`` and leaves ``url`` unset until something actually stores it.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Optional

#: Extension -> artifact kind. The kinds are the closed set the Kanban core
#: accepts (see ``kanban_core.models.Artifact``); anything unrecognised becomes
#: ``other`` rather than being guessed into a wrong bucket.
_KINDS: dict[str, str] = {
    ".json": "json",
    ".jsonl": "json",
    ".log": "log",
    ".txt": "log",
    ".pcap": "pcap",
    ".pcapng": "pcap",
    ".cap": "pcap",
    ".png": "screenshot",
    ".jpg": "screenshot",
    ".jpeg": "screenshot",
    ".gif": "screenshot",
    ".webp": "screenshot",
    ".svg": "screenshot",
    ".md": "report",
    ".html": "report",
    ".pdf": "report",
    ".xml": "scan-output",
    ".nmap": "scan-output",
    ".gnmap": "scan-output",
    ".csv": "scan-output",
}


def kind_for(filename: str) -> str:
    """The artifact kind for a filename, by extension.

    Deliberately conservative: an unknown extension is ``other``, because a file
    mislabelled as a report reads as reviewed evidence when it is neither.
    """
    _, ext = os.path.splitext((filename or "").strip().lower())
    return _KINDS.get(ext, "other")


def artifact_payload(
    *,
    filename: str,
    path: Optional[str] = None,
    size: Optional[int] = None,
    content: Optional[bytes] = None,
    produced_by: Optional[str] = None,
    summary: str = "",
    kind: Optional[str] = None,
) -> dict[str, Any]:
    """Build the artifact body for ``POST /api/cards/{id}/artifacts``.

    Raises ``ValueError`` for an entry that cannot be attached at all, so the
    route can answer 400 with a real reason instead of forwarding a malformed
    artifact that the core would reject with something less useful.
    """
    name = (filename or "").strip()
    if not name:
        raise ValueError("filename is required")
    if size is not None and size < 0:
        raise ValueError("size cannot be negative")

    payload: dict[str, Any] = {
        "name": name,
        "kind": kind or kind_for(name),
        "path": path or name,
        "bytes": int(size or 0),
        "summary": summary or f"attached from the Hermes file manager ({name})",
    }
    if produced_by:
        payload["produced_by"] = produced_by
    if content is not None:
        # Only when the caller actually handed us the bytes. A fabricated hash
        # would be worse than none: the audit trail would assert an integrity
        # property nothing ever checked.
        payload["sha256"] = hashlib.sha256(content).hexdigest()
        payload["bytes"] = len(content)
    return payload


def entry_from_drop(drop: dict[str, Any]) -> dict[str, Any]:
    """Normalise a browser drop payload into a file-manager entry.

    Accepts either the custom ``text/hermes-path`` payload the panel emits or a
    plain ``{name, path, size}`` object, so a drop from outside the panel (a
    real file manager once the GTK shell exists) does not need to be reshaped by
    the caller.
    """
    if not isinstance(drop, dict):
        raise ValueError("drop payload must be an object")
    name = drop.get("name") or drop.get("filename") or drop.get("path") or ""
    path = drop.get("path") or name
    if not name:
        raise ValueError("drop payload carried no filename")
    size = drop.get("size")
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    return {"name": os.path.basename(str(name)), "path": str(path), "size": size}
