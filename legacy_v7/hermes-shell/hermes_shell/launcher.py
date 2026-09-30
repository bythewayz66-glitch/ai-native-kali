"""Application registry and launcher for the Hermes desktop shell.

Phase 7, item 1 - "start-menu integration, the last Phase 6 gap".

The gap Phase 6 left
--------------------
Phase 6 gave the shell a real GTK/window-manager client, but there was no way to
*start* anything from it: no start menu, no registry of what is installed, and no
path from "user picked an app" to "a managed window exists". This module is that
path.

Three sources, one registry
---------------------------
The registry is assembled from three places, and the distinction matters:

* **desktop entries** - real ``.desktop`` files the image ships (Displays,
  terminal, the browser panel). Parsed here rather than shelled out to
  ``gio launch``, so the registry is a value the shell can search, render and
  test without a session bus.
* **tool wrappers** - every tool in ``tool_frontends.registry``. These are what an
  operator actually reaches for on this image, so the launcher is a tool browser
  as much as an app menu. Their tier is carried through so the UI can badge
  intrusive tools rather than presenting ``wifi_deauth`` beside a text editor as
  if they were the same kind of thing.
* **the Hermes session entry** - so the session is discoverable from inside
  itself, which is how a user restarts the panel without a logout.

Why the launch path goes through the state machine
--------------------------------------------------
:meth:`Launcher.launch` opens the window with
``WindowManager.apply("open", ...)`` rather than calling ``open()`` directly.
The registry produces an *intention*; the state machine decides what a window is.
That keeps the "a launched app is a managed window" property enforced in one
place - if the state machine later grows rules (single-instance apps, workspace
placement), the launcher inherits them instead of bypassing them. It also means
the browser surface and the GTK surface launch apps identically.

Search ranking is stated, not tuned
-----------------------------------
Three tiers, first non-empty wins: exact id/name, then prefix match on a word
boundary, then substring anywhere. A fuzzy scorer would rank better on a
large corpus and worse here, because with ~90 entries the failure a user notices
is "I typed `nmap` and it was third", not "it did not find a typo".
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: The three entry kinds. Kept closed so the UI can switch on them exhaustively.
KINDS = ("app", "tool", "session")

#: Tier -> badge label, so an intrusive tool is visibly not a text editor.
TIER_LABELS = {0: "read-only", 1: "low impact", 2: "intrusive", 3: "high impact"}

#: Fallback catalogue shipped when no ``.desktop`` directory is readable (the
#: sandbox case). Mirrors what the image actually installs.
_DEFAULT_DESKTOP_ENTRIES: tuple[dict[str, Any], ...] = (
    {
        "id": "org.kali.hermes.dashboard",
        "name": "Hermes Dashboard",
        "exec": "/usr/bin/hermes-shell-session --panel dashboard",
        "comment": "Board, service health and the model inspector in one surface",
        "categories": ["System", "Monitor"],
        "keywords": ["board", "kanban", "overview", "status", "dashboard"],
    },
    {
        "id": "org.kali.hermes.files",
        "name": "File Manager",
        "exec": "/usr/bin/hermes-files",
        "comment": "Drag a file or a target onto a board card",
        "categories": ["Utility", "FileTools"],
        "keywords": ["files", "browse", "attach", "artifacts", "drop"],
    },
    {
        "id": "org.kali.hermes.terminal",
        "name": "Terminal",
        "exec": "/usr/bin/qterminal",
        "comment": "Shell on the AI-native Kali image",
        "categories": ["System", "TerminalEmulator"],
        "keywords": ["shell", "console", "bash", "terminal"],
    },
    {
        "id": "org.kali.hermes.report",
        "name": "Engagement Report",
        "exec": "/usr/bin/hermes-report",
        "comment": "Assemble the deliverable from card traces and artifacts",
        "categories": ["Office"],
        "keywords": ["report", "findings", "evidence", "deliverable"],
    },
)

#: The Hermes session entry itself, so it is reachable from inside the session.
_SESSION_ENTRY: dict[str, Any] = {
    "id": "org.kali.hermes.session",
    "name": "Hermes AI Desktop",
    "kind": "session",
    "exec": "/usr/bin/hermes-shell-session",
    "comment": "Restart the AI-native desktop session",
    "categories": ["System", "Session"],
    "keywords": ["session", "desktop", "shell", "restart", "hermes"],
}


@dataclass
class LaunchEntry:
    """One launchable thing."""

    id: str
    name: str
    kind: str = "app"
    #: The command the desktop entry declares. Not executed by this module - the
    #: state machine opens a *window*; the session decides what runs in it.
    exec: str = ""
    comment: str = ""
    icon: str = ""
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    #: Guardrail tier for tool entries; ``None`` for plain applications.
    tier: Optional[int] = None
    #: Tool metadata carried through for tool entries.
    binary: str = ""
    requires_scope: bool = False

    @property
    def tier_label(self) -> str:
        if self.tier is None:
            return ""
        return TIER_LABELS.get(int(self.tier), f"T{self.tier}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "exec": self.exec,
            "comment": self.comment,
            "icon": self.icon,
            "categories": list(self.categories),
            "keywords": list(self.keywords),
            "tier": self.tier,
            "tier_label": self.tier_label,
            "binary": self.binary,
            "requires_scope": self.requires_scope,
        }


def parse_desktop_entry(text: str, *, fallback_id: str = "") -> Optional[LaunchEntry]:
    """Parse the ``[Desktop Entry]`` group of a ``.desktop`` file.

    Only the first group is read. Deliberately partial: this needs Name, Exec,
    Comment, Categories and Keywords, and pretending to implement the entire
    Desktop Entry spec (localised keys, Actions, TryExec) would be a larger
    surface than the launcher uses.

    ``fallback_id`` is the file's own name (``org.kali.hermes.files`` for
    ``org.kali.hermes.files.desktop``). The entry id is taken from it in
    preference to slugifying ``Name``, for two reasons that both showed up as
    real failures:

    * **Precedence.** The fallback catalogue keys on the desktop-file id, so a
      shipped file whose ``Name=`` is human-readable ("File Manager") would slug
      to a *different* id and both entries would appear - a duplicate in the
      menu, and the shipped one never actually taking effect.
    * **Stability.** ``Name`` is display text and gets localised; the filename is
      the identity the desktop spec treats as canonical.
    """
    if not text or "[Desktop Entry]" not in text:
        return None
    fields: dict[str, str] = {}
    in_group = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_group = line == "[Desktop Entry]"
            continue
        if not in_group or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields.setdefault(key.strip(), value.strip())

    # Hidden/NoDisplay entries are installed but should not appear in a menu;
    # honouring that is the difference between a launcher and a file dump.
    if fields.get("Hidden", "").lower() == "true" or fields.get("NoDisplay", "").lower() == "true":
        return None
    name = fields.get("Name", "").strip()
    if not name:
        return None
    entry_id = (
        fields.get("X-Hermes-Id")
        or (fallback_id.strip() if fallback_id and fallback_id.strip() else "")
        or re.sub(r"[^a-z0-9._-]+", "-", name.lower())
    )
    return LaunchEntry(
        id=entry_id,
        name=name,
        kind="app",
        exec=fields.get("Exec", "").strip(),
        comment=fields.get("Comment", "").strip(),
        icon=fields.get("Icon", "").strip(),
        categories=[c for c in fields.get("Categories", "").split(";") if c],
        keywords=[k for k in fields.get("Keywords", "").split(";") if k],
    )


def load_desktop_entries(directories: Iterable[str]) -> list[LaunchEntry]:
    """Read every ``.desktop`` file under *directories*. Missing dirs are skipped.

    Skipping rather than raising is deliberate: the same registry code runs on
    the image (where the directories exist) and in the sandbox (where they do
    not), and a launcher that cannot start because a theme directory is absent
    is worse than one with fewer entries.
    """
    entries: list[LaunchEntry] = []
    for directory in directories:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".desktop"):
                continue
            try:
                with open(os.path.join(directory, name), encoding="utf-8") as handle:
                    entry = parse_desktop_entry(handle.read(), fallback_id=name[: -len(".desktop")])
            except OSError:
                continue
            if entry is not None:
                entries.append(entry)
    return entries


def entries_from_tool_specs(specs: Iterable[Any]) -> list[LaunchEntry]:
    """One launch entry per tool wrapper.

    ``specs`` is the ``ToolSpec`` list from ``tool_frontends.registry``. It is
    typed as ``Any`` on purpose: hermes-shell must not import tool-frontends at
    module scope, because the shell has to start on an image where the tool
    service is down. The caller passes the specs in.
    """
    out: list[LaunchEntry] = []
    for spec in specs:
        name = getattr(spec, "name", None)
        if not name:
            continue
        intent = list(getattr(spec, "intent_examples", None) or [])
        out.append(
            LaunchEntry(
                id=f"tool.{name}",
                name=str(name),
                kind="tool",
                exec=f"/usr/bin/{getattr(spec, 'binary', name)}",
                comment=str(getattr(spec, "description", "") or "")[:200],
                icon="applications-security",
                categories=[str(getattr(spec, "category", "") or "security")],
                keywords=intent[:8],
                tier=int(getattr(spec, "tier", 0) or 0),
                binary=str(getattr(spec, "binary", "") or ""),
                requires_scope=bool(getattr(spec, "requires_scope", False)),
            )
        )
    return out


class AppRegistry:
    """The launcher's catalogue: deduplicated, searchable, ordered."""

    def __init__(self, entries: Optional[Iterable[LaunchEntry]] = None) -> None:
        self._entries: dict[str, LaunchEntry] = {}
        for entry in entries or ():
            self.add(entry)

    def add(self, entry: LaunchEntry) -> LaunchEntry:
        """Add an entry. First write wins, so a real ``.desktop`` file is never
        overwritten by the fallback catalogue."""
        self._entries.setdefault(entry.id, entry)
        return entry

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, entry_id: str) -> Optional[LaunchEntry]:
        return self._entries.get(entry_id)

    def entries(self, *, kind: Optional[str] = None) -> list[LaunchEntry]:
        """All entries, sorted by kind then name.

        Sorted rather than insertion-ordered: the menu must look the same on two
        machines that discovered their ``.desktop`` files in different order.
        """
        values = list(self._entries.values())
        if kind:
            values = [e for e in values if e.kind == kind]
        return sorted(values, key=lambda e: (KINDS.index(e.kind) if e.kind in KINDS else 99, e.name.lower()))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in self._entries.values():
            out[entry.kind] = out.get(entry.kind, 0) + 1
        return out

    # -- search ----------------------------------------------------------
    def search(self, query: str, *, limit: int = 20, kind: Optional[str] = None) -> list[LaunchEntry]:
        """Rank entries against *query*. Empty query returns the full list.

        Ranking is three explicit tiers (see the module docstring). Within a tier
        the shorter name wins, because ``nmap`` should beat ``nmap-nse-index``
        for the query ``nmap``.
        """
        candidates = self.entries(kind=kind)
        needle = (query or "").strip().lower()
        if not needle:
            return candidates[:limit]

        scored: list[tuple[int, int, str, LaunchEntry]] = []
        for entry in candidates:
            rank = _rank(entry, needle)
            if rank is None:
                continue
            scored.append((rank, len(entry.name), entry.name.lower(), entry))
        scored.sort(key=lambda row: (row[0], row[1], row[2]))
        return [row[3] for row in scored[:limit]]


def _rank(entry: LaunchEntry, needle: str) -> Optional[int]:
    """0 = exact, 1 = word-boundary prefix, 2 = substring, None = no match."""
    haystacks = [entry.name.lower(), entry.id.lower()]
    if needle in haystacks:
        return 0
    words = re.split(r"[^a-z0-9]+", " ".join(haystacks + [k.lower() for k in entry.keywords]))
    if any(word.startswith(needle) for word in words if word):
        return 1
    haystack = " ".join(haystacks + [k.lower() for k in entry.keywords] + [c.lower() for c in entry.categories])
    return 2 if needle in haystack else None


class Launcher:
    """Binds a registry to the window-manager state machine.

    The only interesting guarantee: a launched entry becomes a **managed
    window** in the same state machine the taskbar, focus and z-order already
    operate on - not a side channel the shell has to special-case.
    """

    def __init__(self, registry: AppRegistry, wm: Any) -> None:
        self.registry = registry
        self.wm = wm
        #: entry id -> window id, so the shell can re-focus a running app
        #: instead of opening a second window for it.
        self.opened: dict[str, str] = {}
        self.launches = 0
        self.refusals = 0

    def launch(self, entry_id: str, *, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Open *entry_id* as a managed window, or raise ``KeyError``.

        An already-open app is focused rather than duplicated - single-instance
        behaviour, which is what a taskbar-based shell implies: two buttons for
        one app is the bug users report as "it opened twice".
        """
        entry = self.registry.get(entry_id)
        if entry is None:
            self.refusals += 1
            raise KeyError(f"no launch entry '{entry_id}'")

        existing = self.opened.get(entry_id)
        if existing and self.wm.get(existing) is not None:
            view = self.wm.apply("focus", {"id": existing})
            return {"entry": entry.as_dict(), "window_id": existing, "view": view, "reused": True}

        view = self.wm.apply(
            "open",
            {
                "app": entry.id,
                "title": entry.name,
                "w": (payload or {}).get("w"),
                "h": (payload or {}).get("h"),
            },
        )
        window_id = (view or {}).get("focused")
        if window_id:
            self.opened[entry_id] = window_id
        self.launches += 1
        return {"entry": entry.as_dict(), "window_id": window_id, "view": view, "reused": False}


def build_registry(
    *,
    desktop_dirs: Optional[Iterable[str]] = None,
    tool_specs: Optional[Iterable[Any]] = None,
    include_fallback: bool = True,
) -> AppRegistry:
    """Assemble the registry from whatever is available.

    Order encodes precedence: real ``.desktop`` files first, then the fallback
    catalogue, then tools, then the session entry. :meth:`AppRegistry.add` keeps
    the first write, so a shipped entry always wins over the fallback.
    """
    registry = AppRegistry()
    found = load_desktop_entries(desktop_dirs or _default_desktop_dirs())
    for entry in found:
        registry.add(entry)
    if include_fallback:
        for raw in _DEFAULT_DESKTOP_ENTRIES:
            registry.add(_entry_from_dict(raw))
    if tool_specs:
        for entry in entries_from_tool_specs(tool_specs):
            registry.add(entry)
    registry.add(_entry_from_dict(_SESSION_ENTRY))
    return registry


def _default_desktop_dirs() -> list[str]:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    data_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    return [os.path.join(data_home, "applications")] + [
        os.path.join(d, "applications") for d in data_dirs if d
    ]


def _entry_from_dict(raw: dict[str, Any]) -> LaunchEntry:
    return LaunchEntry(
        id=raw["id"],
        name=raw["name"],
        kind=raw.get("kind", "app"),
        exec=raw.get("exec", ""),
        comment=raw.get("comment", ""),
        icon=raw.get("icon", ""),
        categories=list(raw.get("categories") or []),
        keywords=list(raw.get("keywords") or []),
    )
