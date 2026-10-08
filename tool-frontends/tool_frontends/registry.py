"""Tool registry: discovery + lookup for every frontend.

Blueprint ref: section 05 - the registry is what the natural-language frontend
(the 'intent -> tool' router) and the MCP server both read from. Adding a Kali
tool frontend is a matter of declaring one :class:`ToolSpec`.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Optional

from .capabilities import build_manifest
from .effects import EFFECTS
from .effects import is_mutating
from .effects import unknown as unknown_effects
from .spec import ToolSpec


#: Function words that carry no tool semantics. Without this filter the router
#: matched on filler: "what cms is this site running" scored for ``hashid``
#: because its example "what type of hash is **this**" shares the words *what*
#: and *this*. Two shared filler words out-voted the one meaningful term.
_STOPWORDS = frozenset(
    """
    what when where which while this that these those then than there here
    with from into your you have has had does did doing will would could
    should about over under some most they them their being been
the and for are was were its it's don't can't please want need help
    """.split()
)


#: Short but high-signal terms that the length filter must not discard.
#:
#: ``_keywords`` drops tokens of 3 characters or fewer because most of them are
#: filler (``the``, ``and``, ``for``). That rule is right for prose and wrong for
#: infrastructure vocabulary, where the *most* discriminating token in a phrase is
#: often the shortest one:
#:
#: * ``is this s3 bucket public`` loses ``s3`` and keeps only ``bucket public``,
#:   which is why the AWS audit tool answered a question about a *public* bucket;
#: * ``audit the aws iam users`` lost both ``aws`` and ``iam`` and matched a local
#:   credential-file audit on the strength of the word ``audit``;
#: * ``check the tls certificate`` lost ``tls`` and was answered by the local
#:   certificate inventory rather than the remote TLS probe.
#:
#: A curated allowlist is used rather than a blanket "keep anything short", because
#: admitting every 2-3 character token re-admits the filler the filter exists to
#: remove. Each term here is a product, protocol or identifier that separates two
#: otherwise similar tools.
_SHORT_TERMS = frozenset(
    """
    s3 ec2 gcs eks gke aks k8s aws gcp azure iam acm kms
    tls ssl ssh rdp smb dns sql ldap vpn waf api url uri cdn
    cve rce xss ssrf lfi sqli c2 arp mac uid gid suid imds
    usb sms qr apt os
    """.split()
)


def _keywords(text: str) -> set[str]:
    """Meaningful tokens from a phrase: not filler, and long enough to discriminate.

    A token survives if it is longer than three characters *or* is a known
    short high-signal term (see :data:`_SHORT_TERMS`).
    """
    return {
        w
        for w in re.findall(r"[a-z0-9_.-]+", (text or "").lower())
        if w not in _STOPWORDS and (len(w) > 3 or w in _SHORT_TERMS)
    }


class ToolRegistry:
    """An ordered, name-indexed collection of tool specs."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool '{spec.name}' is already registered")
        if not 0 <= spec.tier <= 3:
            raise ValueError(f"tool '{spec.name}' has invalid tier {spec.tier}")
        if spec.tier >= 2 and not spec.live_template:
            raise ValueError(f"T{spec.tier} tool '{spec.name}' must declare a live_template")
        if spec.tier >= 2 and not spec.requires_scope:
            raise ValueError(f"T{spec.tier} tool '{spec.name}' must set requires_scope=True")
        if spec.tier >= 3 and not spec.requires_sandbox:
            raise ValueError(f"T3 tool '{spec.name}' must set requires_sandbox=True")
        if not spec.dry_run_template:
            raise ValueError(f"tool '{spec.name}' needs a dry_run_template (safe by default)")
        bad_effects = unknown_effects(spec.effects)
        if bad_effects:
            raise ValueError(
                f"tool '{spec.name}' declares unknown effect(s) {bad_effects}; "
                f"known effects are {list(EFFECTS)}"
            )
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def require(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise KeyError(f"unknown tool '{name}'")
        return spec

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda s: (s.tier, s.category, s.name))

    def by_tier(self, tier: int) -> list[ToolSpec]:
        return [s for s in self.all() if s.tier == tier]

    def by_category(self, category: str) -> list[ToolSpec]:
        return [s for s in self.all() if s.category == category]

    def resolve_intent(self, text: str) -> list[ToolSpec]:
        """Cheap lexical intent router (blueprint 05: natural language -> tool).

        A real deployment fronts this with the local model; the lexical matcher
        keeps the scaffold deterministic and testable.

        Two details turn out to matter for routing quality:

        * filler words are dropped (:data:`_STOPWORDS`) rather than filtered by
          length alone, so common interrogatives cannot add up into a false
          match, and
        * the binary name is matched as a whole **word**, not a raw substring.
          ``"http" in "what cms is this site running"`` is True, which is how
          ``httpx_probe`` used to out-rank ``whatweb_fingerprint`` for a
          question that had nothing to do with HTTP probing.
        """
        needle = (text or "").lower()
        tokens = _keywords(text)

        # A binary name only discriminates between tools that *have different*
        # binaries. ``openssl`` is shared by the remote TLS probe and the local
        # certificate inventory, so seeing it in a phrase tells the router nothing
        # about which one the operator meant - yet a flat +4 made the shared token
        # out-rank the intent examples that did contain the distinguishing words.
        #
        # So the bonus scales down with how many tools claim the same binary, in
        # the same spirit as an inverse document frequency: a token's weight should
        # reflect how much it narrows the field, not merely that it appeared.
        binary_users: dict[str, int] = {}
        for spec in self.all():
            key = spec.binary.lower()
            binary_users[key] = binary_users.get(key, 0) + 1

        scored: list[tuple[int, ToolSpec]] = []
        for spec in self.all():
            score = 0
            if spec.name.replace("_", " ") in needle:
                score += 5
            if spec.binary.lower() in tokens:
                users = binary_users.get(spec.binary.lower(), 1)
                score += 4 if users == 1 else (2 if users == 2 else 1)
            for example in spec.intent_examples:
                score += len(_keywords(example) & tokens)
            if score:
                scored.append((score, spec))
        scored.sort(key=lambda pair: (-pair[0], pair[1].tier))
        return [spec for _, spec in scored]

    def mcp_listing(self) -> list[dict[str, Any]]:
        """MCP-shaped tool list: name, description, JSON-schema parameters."""
        out: list[dict[str, Any]] = []
        for spec in self.all():
            properties: dict[str, Any] = {}
            required: list[str] = []
            for p in spec.params:
                entry: dict[str, Any] = {"type": p.type if p.type != "enum" else "string"}
                if p.description:
                    entry["description"] = p.description
                if p.default is not None:
                    entry["default"] = p.default
                if p.choices:
                    entry["enum"] = p.choices
                properties[p.name] = entry
                if p.required:
                    required.append(p.name)
            out.append(
                {
                    "name": spec.name,
                    "description": f"[T{spec.tier}] {spec.description}",
                    "inputSchema": {"type": "object", "properties": properties, "required": required},
                    "annotations": {
                        "category": spec.category,
                        "tier": spec.tier,
                        "integration": spec.integration,
                        "binary": spec.binary,
                        "requires_scope": spec.requires_scope,
                        "requires_approval": spec.requires_approval,
                        "requires_sandbox": spec.requires_sandbox,
                        # Phase 17: an external MCP client must be able to see what
                        # a tool does to the host, not only how intrusive it is
                        # toward its target. ``effects_enforced`` says whether the
                        # list is the author's declaration or is still empty
                        # pending one - so a client can tell "declares no local
                        # footprint" from "has not declared one yet".
                        "effects": spec.enforced_effects(),
                        "effects_enforced": spec.effects_declared,
                        "effects_inferred": spec.inferred_effects(),
                    },
                }
            )
        return out

    def audit_scope_declarations(self) -> dict[str, Any]:
        """Triage lists of odd scope declarations, for an operator to read.

        This is a *report*, not a gate. Shape-based inference cannot decide the
        question it appears to answer, for two reasons found while writing it:

        * ``target_params`` is overloaded - it holds network targets on some tools
          (``dns_lookup.target``) and local file paths on others (``key_path``,
          ``root_path``, ``binary_path``), so it cannot mean "reaches a target";
          and
        * a target parameter frequently has **no address-shaped default** because
          it is supplied at call time, so 51 tools that correctly declare
          ``requires_scope`` look identical to one that never should have.

        The lists below are therefore emitted for triage and nothing asserts them
        empty. The invariant worth asserting is behavioural and lives in the
        tests: no registered tool may *allow* a live run against an out-of-scope
        host (see ``tests/test_phase16_scope_floor.py``).

        It runs on ``summary()``, which the MCP server and the shell already call,
        so the audit is not something anybody has to remember to run.
        """
        from .targets import classify as classify_target

        flag_without_param: list[str] = []
        param_without_flag: list[str] = []
        for spec in self.all():
            has_address_param = any(
                classify_target(p.default) is not None
                for p in spec.params
                if p.default not in (None, "")
            )
            if spec.requires_scope and not has_address_param:
                flag_without_param.append(spec.name)
            if not spec.requires_scope and has_address_param:
                param_without_flag.append(spec.name)
        offenders = flag_without_param + param_without_flag
        return {
            "checked": len(self.all()),
            "count": len(offenders),
            "offenders": offenders,
            "flag_without_param": flag_without_param,
            "param_without_flag": param_without_flag,
        }

    def audit_effects(self) -> dict[str, Any]:
        """The declared / inferred split - the footprint-migration backlog.

        A report, not a gate. Tools carrying an explicit ``effects`` declaration
        are gated on it; tools without one are gated on nothing but are listed
        here, with the footprint the inference *suggests* they have, so the
        backlog is a number that can be driven down and a wrong inference is
        visible to the author who will confirm it.
        """
        declared: list[str] = []
        inferred: list[str] = []
        suggested_mutators: list[str] = []
        for spec in self.all():
            if spec.effects_declared:
                declared.append(spec.name)
                continue
            inferred.append(spec.name)
            # The *inferred* set is what the suggestion reads. Reading the
            # enforced set here (empty for an undeclared tool) made the list
            # permanently empty - the one signal that tells an author to confirm
            # a footprint was silently reporting nothing.
            if is_mutating(spec.inferred_effects()):
                suggested_mutators.append(spec.name)
        return {
            "checked": len(self.all()),
            "declared": len(declared),
            "inferred": len(inferred),
            "declared_tools": sorted(declared),
            "inferred_tools": sorted(inferred),
            "suggested_mutators": sorted(suggested_mutators),
        }

    def capability_manifest(self) -> dict[str, Any]:
        """The operator-facing statement of everything this layer can do.

        Derived from the same specs the guardrails read, so the manifest cannot
        describe a policy the enforcement path does not apply.
        """
        return build_manifest(self)

    def summary(self) -> dict[str, Any]:
        tools = self.all()
        audit = self.audit_scope_declarations()
        effects_report = self.audit_effects()
        return {
            "count": len(tools),
            "by_tier": {t: len(self.by_tier(t)) for t in range(4)},
            "by_category": {c: len(self.by_category(c)) for c in sorted({s.category for s in tools})},
            # Phase 16: the scope audit rides along with the counts, so a summary
            # read of the registry cannot be mistaken for a healthy one.
            "scope_declarations": {"checked": audit["checked"], "offenders": audit["count"]},
            # Phase 17: the same for the local-footprint (effects) audit.
            "effects": {
                "checked": effects_report["checked"],
                "declared": effects_report["declared"],
                "inferred": effects_report["inferred"],
            },
        }


#: Process-wide registry, populated with the built-in Kali frontends.
REGISTRY = ToolRegistry()

def get_registry() -> ToolRegistry:
    """Return the populated registry, loading built-ins on first use."""
    if not REGISTRY.all():
        from .wrappers import register_builtin

        register_builtin(REGISTRY)
    return REGISTRY
