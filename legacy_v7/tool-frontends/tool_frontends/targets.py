"""Target classification - which arguments are network addresses?

Workstream C, Phase 3.

The bug this module exists to prevent
-------------------------------------
Scope enforcement used to ask one question: *is the value of this tool's
declared ``target_params`` inside the scope?* A spec declares its target
parameters **by hand**, so a spec carrying an address in a *second* parameter
was never checked at all. ``dns_zone_transfer`` takes both ``target`` (the
domain) and ``nameserver`` (the host that is actually queried). The AXFR goes to
``nameserver`` -- but only ``target`` was compared against the scope. Point
``nameserver`` at a host outside the authorization and the tool dutifully sends
it a zone-transfer request.

That is a **target escape**: a parameter that reaches an out-of-scope host
without ever being compared against the scope.

Two ways to close it
--------------------
1. Declare the extra parameter (``target_params=["nameserver"]``). Necessary,
   but not sufficient -- it relies on the author remembering, and an author who
   forgets opens the hole again silently.
2. **Classify every argument by its value** and refuse anything that resolves to
   a network identifier and is not covered by the scope.

This module is (2). Both layers now run: declared parameters must be covered,
*and* no argument may name an out-of-scope address. A spec author who forgets to
declare a parameter no longer opens a hole, because the classifier catches it.

What counts as an address
-------------------------
The classifier is deliberately biased toward **recall** on anything that
resolves to a network endpoint -- an IPv4/IPv6 literal, a CIDR, a URL, a
MAC/BSSID, an FQDN, an email or a phone identifier -- because a false positive
costs one explicit conversation with the operator, while a false negative sends
traffic to a host nobody authorised.

It is equally deliberately careful about **local** values. A file path, a
wordlist name, an enum choice or a bare word like ``root`` is not an address.
Classifying those as addresses would make the control noisy enough that an
operator would switch it off, and a control that gets switched off protects
nobody.

Known limit
-----------
A **bare single-label hostname** (``fileserver``, no dot, no digit) is not
classified, because the same shape covers ``root``, ``admin``, ``ssh`` and
``top100`` -- parameters that are demonstrably not hosts. Such a value is still
protected whenever it is the declared target parameter, and
``tests/test_target_escapes.py`` fails the build if any wrapper has an
address-shaped parameter that is not declared. The runtime classifier and the
authoring lint close the gap between them; neither is sufficient alone.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Optional

#: What kind of network identifier a value looks like.
TargetKind = str  # one of the KINDS below

KINDS = (
    "ipv4",
    "ipv6",
    "cidr",
    "mac",
    "url",
    "hostname",
    "email",
    "phone",
    "onion",
)

#: File extensions that mark a value as a local path rather than a hostname.
#:
#: Without this, ``rockyou.txt`` classifies as the domain ``rockyou`` under the
#: ``.txt`` TLD, and every wordlist parameter would be refused. Checking the
#: extension is cruder than checking the filesystem, but it is deterministic and
#: does not depend on the file existing on the build host.
_FILE_EXTENSIONS = frozenset(
    """
    txt json yaml yml toml ini conf cfg pem crt cer key pub sh bash py rb pl js
    mjs ts tsx bin img iso elf exe dll so dylib md csv tsv xml log db sqlite
    sqlite3 pcap pcapng cap har html htm pdf docx doc xlsx xls zip gz bz2 xz
    tar 7z deb rpm apk pkg whl egg list dic rules
    """.split()
)

#: Path prefixes and shapes that are unambiguously local.
_PATH_HINTS = ("/", "./", "../", "~/", "\\", "\\\\")

_MAC_RE = re.compile(r"^(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,24}$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\-\s()]{5,18}[0-9]$")
#: FQDN: at least one dot, plausible labels, alphabetic TLD.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)"
    r"(?:[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)+"
    r"[A-Za-z]{2,24}\.?$"
)
_ONION_RE = re.compile(r"^[a-z2-7]{16,56}\.onion$", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
#: ``host:port`` where host is a bare single label - common in scan arguments.
_HOSTPORT_RE = re.compile(r"^(?P<host>[A-Za-z0-9_.-]{2,253}):(?P<port>[0-9]{1,5})$")


@dataclass(frozen=True)
class FoundTarget:
    """One address-shaped argument value."""

    key: str
    value: str
    kind: TargetKind
    declared: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "kind": self.kind,
            "declared": self.declared,
        }


def _has_file_extension(value: str) -> bool:
    tail = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in tail:
        return False
    ext = tail.rsplit(".", 1)[-1].lower()
    return ext in _FILE_EXTENSIONS


def _looks_like_path(value: str) -> bool:
    if value.startswith(_PATH_HINTS):
        return True
    # A bare filename with a known extension is a path, not a hostname.
    return _has_file_extension(value) and "://" not in value


def _strip_port(value: str) -> str:
    """Drop a trailing ``:port`` from a host-shaped value, keeping IPv6 intact."""
    if value.startswith("["):  # bracketed IPv6 literal
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        if port.isdigit():
            return host
    return value


def classify(value: Any) -> Optional[TargetKind]:
    """Return the address kind of *value*, or ``None`` if it is not an address.

    Order matters: the structured forms (MAC, URL, IPv6, CIDR, IPv4, email,
    phone) are checked before the permissive FQDN pattern, so a value is
    classified by its most specific shape rather than the loosest one it
    happens to match.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > 253:
        return None
    if _looks_like_path(raw):
        return None

    # -- media access control (BSSID / station MAC) --------------------------
    if _MAC_RE.match(raw):
        return "mac"

    # -- URL / URI ----------------------------------------------------------
    if _SCHEME_RE.match(raw):
        return "url"

    # -- Tor hidden service -------------------------------------------------
    if _ONION_RE.match(raw):
        return "onion"

    # -- IPv6 (bare or bracketed) -------------------------------------------
    candidate = _strip_port(raw)
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        ip = None
    if ip is not None:
        return "ipv6" if ip.version == 6 else "ipv4"

    # -- CIDR ---------------------------------------------------------------
    if "/" in raw:
        left, _, right = raw.partition("/")
        try:
            if ipaddress.ip_network(raw, strict=False) and left and right:
                return "cidr"
        except ValueError:
            pass

    # -- email / phone ------------------------------------------------------
    if _EMAIL_RE.match(raw):
        return "email"
    if _PHONE_RE.match(raw):
        return "phone"

    # -- FQDN, optionally with a port ---------------------------------------
    # ``mail.example.net:25`` is a normal scan argument, so the port is stripped
    # before the FQDN pattern is applied. A *single-label* host with a port
    # (``fileserver:8080``) still falls through, for the reason documented in
    # the module docstring - and the authoring lint covers that case instead.
    host_part = raw
    match = _HOSTPORT_RE.match(raw)
    if match:
        host_part = match.group("host")
    if _HOSTNAME_RE.match(host_part):
        return "hostname"

    return None


def normalize(value: str, kind: Optional[TargetKind] = None) -> str:
    """Reduce an address to the host form used for scope comparison."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if kind is None:
        kind = classify(raw)
    if kind in ("url", "hostname", "onion", "email"):
        if _SCHEME_RE.match(raw):
            raw = raw.split("://", 1)[1]
        for sep in ("/", "?", "#"):
            if sep in raw:
                raw = raw.split(sep, 1)[0]
        if kind == "email":
            raw = raw.split("@", 1)[-1]
        raw = _strip_port(raw)
    return raw


def scan(
    args: dict[str, Any],
    *,
    declared_params: tuple[str, ...] | list[str] = ("target",),
    skip_keys: tuple[str, ...] | list[str] = (),
) -> list[FoundTarget]:
    """Find every address-shaped argument value.

    ``declared`` records whether the parameter was named in the spec's
    ``target_params``. An undeclared hit is exactly the escape class that
    motivated this module.
    """
    declared = {p for p in declared_params}
    skip = {k for k in skip_keys}
    found: list[FoundTarget] = []
    for key, value in (args or {}).items():
        if key.startswith("__") or key in skip:
            continue
        kind = classify(value)
        if kind is None:
            continue
        found.append(
            FoundTarget(key=key, value=str(value).strip(), kind=kind, declared=key in declared)
        )
    found.sort(key=lambda f: (f.declared, f.key))
    return found


def escapes(
    args: dict[str, Any],
    *,
    scope: Any,
    declared_params: tuple[str, ...] | list[str] = ("target",),
    skip_keys: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Every address-shaped argument that the scope does not cover.

    This is the value-based half of scope enforcement. It is deliberately
    independent of what the spec declares, so forgetting a declaration can no
    longer send traffic to an unauthorised host.
    """
    if scope is None or not (getattr(scope, "targets", None) or getattr(scope, "cidrs", None)):
        return []
    reasons: list[str] = []
    for hit in scan(args, declared_params=declared_params, skip_keys=skip_keys):
        candidate = normalize(hit.value, hit.kind)
        if not candidate:
            continue
        if scope.covers(candidate):
            continue
        marker = "" if hit.declared else " (undeclared parameter)"
        reasons.append(
            f"argument '{hit.key}' names '{hit.value}' ({hit.kind}) which is outside the "
            f"authorized scope ({scope.summary()}){marker}"
        )
    return reasons
