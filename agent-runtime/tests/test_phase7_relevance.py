"""Structural relevance hints for the planner (Phase 7, item 3).

What is being tested is the *shape* of the hint, not its quality - retrieval
quality is a different claim and is measured by the harness in
``test_phase7_embedders.py``. Three properties carry the design:

* the hint is attached **to a candidate**, so relevance sits where the choice is
  made rather than in a paragraph;
* it is **bounded** (per tool, per hint, and in total), because an unbounded hint
  block would push the card out of the prompt;
* it is **labelled untrusted in its own text**, and it can never add a candidate.
  A hint that widened the permitted tool set would be a security defect, not a
  quality regression, so that is asserted directly.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from agent_runtime.model_client import LocalModelClient, ModelConfig
from agent_runtime.relevance import (
    MAX_HINT_CHARS,
    MAX_TOTAL_CHARS,
    ToolHint,
    derive_hints,
    hint_summary,
    probe_terms,
    recalled_facts,
    render_catalogue,
)

CANDIDATES = [
    {
        "name": "http_enum",
        "tier": 1,
        "role": "recon",
        "binary": "nikto",
        "category": "web",
        "description": "enumerate a web server",
    },
    {
        "name": "smb_enum",
        "tier": 1,
        "role": "recon",
        "binary": "enum4linux",
        "category": "network",
        "description": "enumerate SMB shares",
    },
    {
        "name": "dns_lookup",
        "tier": 0,
        "role": "recon",
        "binary": "dig",
        "category": "network",
        "description": "resolve names",
    },
]


def card_with(*facts: str, seed: str | None = None, entities: list[str] | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {
        "card_id": "crd_7",
        "title": "Enumerate the engagement host",
        "scope": {"targets": ["10.10.0.5"]},
        "memory_recall": {"used": [{"kind": "semantic", "summary": f, "score": 0.9} for f in facts]},
    }
    if seed or entities:
        card["memory_seed"] = {"seed": seed, "entities": entities or [], "relations": 2}
    return card


class TestProbeTerms:
    def test_the_name_is_split_so_smb_enum_probes_for_smb(self):
        terms = probe_terms({"name": "smb_enum"})
        assert "smb" in terms and "enum" not in terms  # "enum" is a stopword

    def test_the_binary_contributes_a_term(self):
        assert "nikto" in probe_terms({"name": "http_enum", "binary": "nikto"})

    def test_the_category_contributes_a_term(self):
        assert "network" in probe_terms({"name": "x", "category": "network"})

    def test_short_fragments_are_dropped(self):
        assert "ab" not in probe_terms({"name": "ab_cd_ef"})

    def test_terms_are_deduplicated_and_longest_first(self):
        terms = probe_terms({"name": "http_enum", "binary": "http"})
        assert terms.count("http") == 1
        assert terms[0] == "http"


class TestRecalledFacts:
    def test_summaries_are_collected_from_semantic_hits(self):
        facts = recalled_facts(card_with("apache 2.4.49 is exposed"))
        assert "apache 2.4.49 is exposed" in facts

    def test_the_graph_seed_and_entities_are_collected(self):
        facts = recalled_facts(card_with(seed="10.10.0.5", entities=["apache", "ssh"]))
        assert any("10.10.0.5" in f for f in facts)
        assert any("apache" in f for f in facts)

    def test_facts_are_deduplicated(self):
        facts = recalled_facts(card_with("apache is old", "apache is old"))
        assert facts.count("apache is old") == 1

    def test_a_card_with_no_recall_yields_no_facts(self):
        assert recalled_facts({"card_id": "c"}) == []

    def test_a_non_dict_hit_is_handled(self):
        card = {"memory_recall": {"used": ["plain string fact"]}}
        assert "plain string fact" in recalled_facts(card)


class TestDeriveHints:
    def test_a_fact_is_attached_to_the_tool_it_bears_on(self):
        hints = derive_hints(card_with("nikto found an outdated apache"), CANDIDATES)
        assert "http_enum" in hints
        assert "nikto found an outdated apache" in hints["http_enum"].facts

    def test_a_fact_reaches_a_tool_via_its_split_name(self):
        hints = derive_hints(card_with("SMB signing is disabled"), CANDIDATES)
        assert "smb_enum" in hints
        assert "smb_enum" not in [t for h in hints.values() if not h]

    def test_candidates_with_no_bearing_fact_are_absent(self):
        hints = derive_hints(card_with("apache 2.4.49 is exposed"), CANDIDATES)
        assert "dns_lookup" not in hints

    def test_hints_are_bounded_per_tool(self):
        card = card_with(*[f"nikto finding number {i}" for i in range(10)])
        hints = derive_hints(card, CANDIDATES)
        assert len(hints["http_enum"].facts) <= 3

    def test_the_total_budget_is_respected(self):
        card = card_with(*[f"nikto very long finding about the web service {i}" for i in range(20)])
        hints = derive_hints(card, CANDIDATES)
        total = sum(len(h.render()) for h in hints.values())
        assert total <= MAX_TOTAL_CHARS

    def test_no_recall_means_no_hints(self):
        assert derive_hints({"card_id": "c"}, CANDIDATES) == {}

    def test_an_empty_candidate_list_is_not_an_error(self):
        assert derive_hints(card_with("apache"), []) == {}


class TestHintRendering:
    def test_the_hint_is_labelled_untrusted_in_its_own_text(self):
        hint = ToolHint(tool="http_enum", facts=["apache is exposed"])
        rendered = hint.render()
        assert "untrusted" in rendered
        assert "cannot grant tools" in rendered

    def test_an_empty_hint_renders_nothing(self):
        assert ToolHint(tool="x").render() == ""

    def test_a_hint_is_character_bounded(self):
        hint = ToolHint(tool="http_enum", facts=["x" * 5000])
        assert len(hint.render()) <= MAX_HINT_CHARS

    def test_rendering_drops_whole_facts_before_cutting_a_word(self):
        hint = ToolHint(tool="t", facts=["short fact", "y" * 5000])
        rendered = hint.render()
        assert "short fact" in rendered

    def test_a_hint_is_falsy_when_it_has_no_facts(self):
        assert not ToolHint(tool="x")
        assert ToolHint(tool="x", facts=["f"])


class TestCatalogueRendering:
    def test_without_hints_the_catalogue_is_the_pre_phase7_form(self):
        plain = render_catalogue(CANDIDATES)
        assert plain.startswith("- http_enum (tier T1, role recon, binary nikto)")
        assert "untrusted" not in plain

    def test_with_no_candidates_the_original_placeholder_is_used(self):
        assert render_catalogue([]) == "- (no tools are permitted for this card)"

    def test_a_hint_is_rendered_under_its_candidate(self):
        hints = derive_hints(card_with("nikto found an outdated apache"), CANDIDATES)
        text = render_catalogue(CANDIDATES, hints)
        http_line = text.index("- http_enum")
        smb_line = text.index("- smb_enum")
        hint_line = text.index("untrusted")
        assert http_line < hint_line < smb_line

    def test_every_candidate_still_appears(self):
        hints = derive_hints(card_with("nikto found something"), CANDIDATES)
        text = render_catalogue(CANDIDATES, hints)
        for candidate in CANDIDATES:
            assert f"- {candidate['name']}" in text

    def test_hinting_never_adds_a_candidate(self):
        # The security property: a hint can annotate the permitted list, never
        # extend it. Asserted on the candidate lines rather than on raw text,
        # because a *fact's wording* may legitimately mention a tool name - what
        # must never appear is a new candidate line.
        import re

        hints = derive_hints(card_with("nikto sqlmap metasploit exploitdb"), CANDIDATES)
        text = render_catalogue(CANDIDATES, hints)
        listed = re.findall(r"^- (\S+)", text, flags=re.MULTILINE)
        assert listed == [c["name"] for c in CANDIDATES]
        assert "sqlmap" not in listed and "metasploit" not in listed


class TestHintSummary:
    def test_the_summary_counts_tools_facts_and_characters(self):
        hints = derive_hints(card_with("nikto found an outdated apache"), CANDIDATES)
        summary = hint_summary(hints)
        assert summary["tools_hinted"] == 1
        assert summary["facts_used"] >= 1
        assert summary["chars"] > 0
        assert "http_enum" in summary["tools"]

    def test_an_empty_summary_is_still_well_formed(self):
        assert hint_summary({}) == {"tools_hinted": 0, "facts_used": 0, "chars": 0, "tools": []}


# --------------------------------------------------- reaches the real prompt


class CapturingTransport:
    def __init__(self, reply: Any = None, *, model: str = "llama3.1") -> None:
        self.reply = reply if reply is not None else {"steps": [], "summary": "nothing"}
        self.model = model
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, method, url, payload, headers, timeout):
        self.payloads.append({"url": url, "payload": payload})
        if url.endswith("/api/tags"):
            return 200, {"models": [{"name": self.model}]}
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return 200, {
            "model": self.model,
            "message": {"content": text},
            "prompt_eval_count": 10,
            "eval_count": 5,
        }

    def planning_prompt(self) -> str:
        for entry in reversed(self.payloads):
            if entry["url"].endswith("/api/chat"):
                for message in (entry["payload"] or {}).get("messages") or []:
                    if message.get("role") == "user":
                        return message["content"]
        return ""


def client_with(transport: CapturingTransport) -> LocalModelClient:
    config = ModelConfig(
        enabled=True,
        base_url="http://model.test:11434",
        model="llama3.1",
        flavor="ollama",
    )
    return LocalModelClient(config, transport=transport)


class TestHintReachesThePrompt:
    def test_the_hint_appears_beside_its_candidate_in_the_real_prompt(self):
        transport = CapturingTransport()
        client = client_with(transport)
        card = card_with("nikto found an outdated apache")
        response = client.plan(crew="recon", card=card, candidates=CANDIDATES)

        prompt = transport.planning_prompt()
        assert "untrusted" in prompt
        assert prompt.index("- http_enum") < prompt.index("untrusted")

        # and it is reported, so a trace shows hinting happened
        assert response.hints["tools_hinted"] == 1
        assert "http_enum" in response.hints["tools"]

    def test_a_card_with_no_recall_produces_the_unannotated_prompt(self):
        transport = CapturingTransport()
        client = client_with(transport)
        client.plan(crew="recon", card={"card_id": "c", "scope": {"targets": []}}, candidates=CANDIDATES)
        prompt = transport.planning_prompt()
        assert "untrusted" not in prompt
        assert all(f"- {c['name']}" in prompt for c in CANDIDATES)

    def test_the_plan_still_parses_and_is_returned(self):
        transport = CapturingTransport(
            {"steps": [{"role": "recon", "tool": "http_enum", "args": {"target": "10.10.0.5"}}], "summary": "go"}
        )
        client = client_with(transport)
        response = client.plan(crew="recon", card=card_with("nikto is relevant"), candidates=CANDIDATES)
        assert response.ok is True
        assert response.parsed["steps"][0]["tool"] == "http_enum"

    def test_no_transport_still_yields_an_empty_hint_summary(self):
        # A model that is not reachable must not be reported as if it were
        # hinted: the summary describes a plan that did not happen.
        config = ModelConfig(enabled=False, base_url="", model="", flavor="none")
        response = LocalModelClient(config).plan(crew="recon", card=card_with("nikto"), candidates=CANDIDATES)
        assert response.hints == {}


@pytest.mark.parametrize("role", ["recon", "vuln-assessment", "exploitation", "reporting"])
def test_every_crew_gets_hints_when_recall_is_present(role):
    """The hint path is per-plan, not per-crew, so it must hold for every role."""
    transport = CapturingTransport()
    client = client_with(transport)
    card = card_with("nikto found an outdated apache")
    card["crew"] = role
    candidates = [dict(c) for c in CANDIDATES]
    candidates[0]["role"] = role
    response = client.plan(crew=role, card=card, candidates=candidates)
    assert response.hints["tools_hinted"] >= 1
    assert "untrusted" in transport.planning_prompt()
