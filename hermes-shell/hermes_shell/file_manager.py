"""File-manager browse surface for the Hermes desktop shell.

The shell can launch applications and manage windows, but there was no surface
for *looking at files* - the operator's most common desktop action, and the one
an agent layer most needs to be able to narrate ("the scan wrote 3 reports under
/root/engagements").

Browse only, deliberately
-------------------------
This module **reads**. It has no write, move, rename or delete operation. That is
a scope decision, not an unfinished one: a browse surface that can also destroy
files needs an approval gate on every destructive call, and the shell already has
a card-based approval path for work that changes state. A read-only surface can be
handed to the agent layer without that gate, which is exactly what makes it useful
here.

The sandbox boundary is the whole security story
------------------------------------------------
A file browser is an arbitrary-file-read primitive, so the boundary is the feature:

* every path is resolved with ``os.path.realpath`` **before** the check, so ``..``
  segments and symlinks are already collapsed by the time the prefix test runs -
  checking the *requested* path would pass ``/root/link-out`` where ``link-out``
  points at ``/etc/shadow``;
* the resolved path must sit under one of the configured roots, compared with a
  trailing separator so ``/root`` does not authorise ``/rootkit``; and
* a refused path returns a reason, never a partial listing - a caller must not be
  able to infer directory contents from an error.

Roots default to the places an operator actually browses on this image rather than
to ``/``. A root of ``/`` would make every one of the checks above decorative.
"""
from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass, field
from typing import Any, Optional

#: Where a browse may go unless the caller configures otherwise. Not ``/``:
#: see the module docstring - a root of ``/`` makes the boundary decorative.
DEFAULT_ROOTS: tuple[str, ...] = ("/root", "/home", "/etc", "/var/log", "/tmp")

#: A preview read is capped. A file manager that will happily read a 4 GB log
#: into an HTTP response is a denial-of-service against its own shell.
MAX_PREVIEW_BYTES = 256 * 1024

#: Directory listings are capped for the same reason. ``truncated`` says so
#: explicitly, so the UI never silently shows a partial view as the whole one.
MAX_ENTRIES = 2000


class BrowseRefused(Exception):
    """A path fell outside the sandbox. Carries the reason, never the contents."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Entry:
    """One directory entry, with just enough to render a row."""

    name: str
    path: str
    kind: str  # file | dir | link | other
    size: Optional[int] = None
    mtime: Optional[float] = None
    readable: bool = False
    target: Optional[str] = None  # for a symlink, where it points

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            "mtime": self.mtime,
            "readable": self.readable,
            "target": self.target,
        }


@dataclass
class FileManager:
    """Read-only, root-confined filesystem browsing for the shell.

    ``roots`` is normalised once, at construction: a root that is itself a
    symlink (``/home`` can be) would otherwise never match a ``realpath``-resolved
    candidate, and the surface would refuse everything under it.
    """

    roots: tuple[str, ...] = DEFAULT_ROOTS
    max_preview_bytes: int = MAX_PREVIEW_BYTES
    max_entries: int = MAX_ENTRIES

    def __post_init__(self) -> None:
        normalised: list[str] = []
        for root in self.roots:
            real = os.path.realpath(os.path.expanduser(root))
            normalised.append(real.rstrip("/") or "/")
        # Longest first: an exact-prefix check against several roots must test the
        # most specific one first, so /home/user wins over /home for a message.
        self._roots: tuple[str, ...] = tuple(sorted(set(normalised), key=len, reverse=True))

    # -- the boundary ----------------------------------------------------
    @property
    def normalised_roots(self) -> list[str]:
        """The roots as they are actually compared. Exposed for the UI's tree root."""
        return list(self._roots)

    def resolve(self, path: str) -> str:
        """Canonicalise *path* and prove it is inside a root, or raise.

        The ``realpath`` before the check is the load-bearing part - see the module
        docstring. It resolves ``..`` *and* follows symlinks, so both escape routes
        are collapsed before anything compares prefixes.
        """
        if not path or not isinstance(path, str):
            raise BrowseRefused("a path is required")
        candidate = path if os.path.isabs(path) else os.path.join(self._roots[-1], path)
        # expanduser first: ``~`` is how an operator actually types a home path,
        # and refusing it would be a usability bug with a security alibi.
        candidate = os.path.expanduser(candidate)
        real = os.path.realpath(candidate)
        for root in self._roots:
            if real == root:
                return real
            prefix = root if root.endswith("/") else root + "/"
            if real.startswith(prefix):
                return real
        raise BrowseRefused(
            f"path is outside the browsable roots ({', '.join(self._roots)})"
        )

    # -- reads -----------------------------------------------------------
    def list_dir(self, path: str) -> dict[str, Any]:
        """List a directory, directories first then case-insensitive by name.

        Sorting *here* rather than leaving it to the client matters: every
        renderer needs the same order, and two sort implementations is how a list
        view and a tree view come to disagree about what is at the top.
        """
        real = self.resolve(path)
        if not os.path.isdir(real):
            raise BrowseRefused("not a directory")
        entries: list[Entry] = []
        try:
            names = sorted(os.listdir(real))
        except PermissionError as exc:
            raise BrowseRefused(f"permission denied: {exc.strerror or 'denied'}") from exc
        truncated = len(names) > self.max_entries
        for name in names[: self.max_entries]:
            full = os.path.join(real, name)
            try:
                info = os.lstat(full)
            except OSError:
                # A dangling symlink or a racing delete: list it as unreadable
                # rather than dropping it, so the view matches what is on disk.
                entries.append(Entry(name=name, path=full, kind="other"))
                continue
            is_link = stat_module.S_ISLNK(info.st_mode)
            if is_link:
                kind = "link"
            elif stat_module.S_ISDIR(info.st_mode):
                kind = "dir"
            elif stat_module.S_ISREG(info.st_mode):
                kind = "file"
            else:
                kind = "other"
            target = None
            if is_link:
                try:
                    target = os.readlink(full)
                except OSError:
                    target = None
            entries.append(
                Entry(
                    name=name,
                    path=full,
                    kind=kind,
                    size=None if kind == "dir" else info.st_size,
                    mtime=info.st_mtime,
                    readable=os.access(full, os.R_OK),
                    target=target,
                )
            )
        entries.sort(key=lambda e: (e.kind != "dir", e.name.lower()))
        return {
            "path": real,
            "parent": self._parent_of(real),
            "roots": list(self._roots),
            "entries": [e.as_dict() for e in entries],
            "count": len(entries),
            "truncated": truncated,
        }

    def _parent_of(self, real: str) -> Optional[str]:
        """The parent directory if it is still inside a root, else ``None``.

        ``None`` is what makes "up" at a root boundary do nothing in the UI, which
        is better than a parent link that 404s when clicked.
        """
        parent = os.path.dirname(real)
        if parent == real:
            return None
        try:
            return self.resolve(parent)
        except BrowseRefused:
            return None

    def stat(self, path: str) -> dict[str, Any]:
        real = self.resolve(path)
        try:
            info = os.stat(real)
        except FileNotFoundError as exc:
            raise BrowseRefused("no such file or directory") from exc
        except PermissionError as exc:
            raise BrowseRefused(f"permission denied: {exc.strerror or 'denied'}") from exc
        return {
            "path": real,
            "kind": (
                "dir" if stat_module.S_ISDIR(info.st_mode)
                else "file" if stat_module.S_ISREG(info.st_mode)
                else "other"
            ),
            "size": info.st_size,
            "mtime": info.st_mtime,
            "mode": oct(stat_module.S_IMODE(info.st_mode)),
            "readable": os.access(real, os.R_OK),
        }

    def read_preview(self, path: str, *, max_bytes: Optional[int] = None) -> dict[str, Any]:
        """Read the head of a text file, for a preview pane.

        Refuses binary content rather than returning mojibake. A file manager that
        renders a truncated ELF as replacement characters teaches the operator
        nothing, and the honest answer - "this is not text" - is cheaper to act on.
        """
        real = self.resolve(path)
        if not os.path.isfile(real):
            raise BrowseRefused("not a regular file")
        cap = int(max_bytes or self.max_preview_bytes)
        cap = max(1, min(cap, self.max_preview_bytes))
        size = os.path.getsize(real)
        with open(real, "rb") as handle:
            raw = handle.read(cap + 1)
        truncated = len(raw) > cap
        chunk = raw[:cap]
        if b"\x00" in chunk:
            raise BrowseRefused("not a text file (contains NUL bytes)")
        return {
            "path": real,
            "size": size,
            "bytes_read": len(chunk),
            "truncated": truncated or size > len(chunk),
            "text": chunk.decode("utf-8", errors="replace"),
        }

    def search(self, path: str, query: str, *, limit: int = 200) -> dict[str, Any]:
        """Case-insensitive substring match on names, one level deep.

        One level deep on purpose: a recursive walk of ``/var/log`` is an
        unbounded amount of work inside a request handler. A caller that wants
        recursion asks repeatedly, per directory, and stays responsive.
        """
        real = self.resolve(path)
        needle = (query or "").lower()
        if not needle:
            raise BrowseRefused("a query is required")
        listing = self.list_dir(real)
        hits = [e for e in listing["entries"] if needle in e["name"].lower()][:limit]
        return {
            "path": real,
            "query": query,
            "matches": hits,
            "count": len(hits),
        }

    def status(self) -> dict[str, Any]:
        """What the UI needs to build the tree root and grey out the rest."""
        roots = []
        for root in self._roots:
            roots.append(
                {
                    "path": root,
                    "exists": os.path.isdir(root),
                    "readable": os.path.isdir(root) and os.access(root, os.R_OK),
                }
            )
        return {
            "roots": roots,
            "read_only": True,
            "max_preview_bytes": self.max_preview_bytes,
            "max_entries": self.max_entries,
        }
