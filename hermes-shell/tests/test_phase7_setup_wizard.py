"""First-boot setup wizard tests (Phase 7, item 6).

The claim under test is not "it collects answers" - it is that the wizard is
**honest**: it skips steps it cannot usefully ask, it does not promise capability
the host lacks, and it refuses to write anything implicitly. Each of those is a
test below, because each is a way the surface could start lying.
"""
from __future__ import annotations

import pytest

from hermes_shell.setup_wizard import (
    ENV_KEYS,
    STEPS,
    Readiness,
    SetupWizard,
    probe_readiness,
)


class TestStepModel:
    def test_the_first_step_is_the_engagement(self):
        """Authorisation precedes everything: scope changes what later answers
        mean, so collecting preferences first would invert the order."""
        assert STEPS[0].key == "engagement"

    def test_every_step_key_maps_to_an_env_var(self):
        assert {t.key for t in STEPS} == set(ENV_KEYS)

    def test_step_keys_are_unique(self):
        keys = [t.key for t in STEPS]
        assert len(keys) == len(set(keys))

    def test_each_steps_prerequisite_exists_and_comes_earlier(self):
        order = [t.key for t in STEPS]
        for step in STEPS:
            if step.requires is not None:
                assert step.requires in order, f"{step.key} requires unknown {step.requires}"
                assert order.index(step.requires) < order.index(step.key), (
                    f"{step.key} requires {step.requires}, which is asked later"
                )


class TestPending:
    def test_only_the_opening_steps_are_offered_first(self):
        # With nothing answered only the engagement is askable: scope, embedder
        # and telemetry are all about *this* engagement, so asking them first
        # would invert the order.
        wizard = SetupWizard()
        pending = {s.key for s in wizard.pending()}
        assert pending == {"engagement"}

    def test_a_dependent_step_appears_only_after_its_prerequisite(self):
        wizard = SetupWizard()
        assert "model_name" not in {s.key for s in wizard.pending()}
        wizard.answer("engagement", "acme")
        wizard.answer("model_url", "http://127.0.0.1:11434")
        assert "model_name" in {s.key for s in wizard.pending()}

    def test_a_blank_prerequisite_hides_the_dependent_step(self):
        """"No model" must not produce a phantom model name."""
        wizard = SetupWizard()
        wizard.answer("engagement", "acme")
        wizard.answer("model_url", "")
        assert "model_name" not in {s.key for s in wizard.pending()}

    def test_an_answered_step_is_not_offered_again(self):
        wizard = SetupWizard()
        wizard.answer("engagement", "acme")
        assert "engagement" not in {s.key for s in wizard.pending()}

    def test_the_scope_step_survives_any_engagement(self):
        """Scope has a prerequisite but no default: it must be answered by a
        person, so it stays pending however the rest is filled in."""
        wizard = SetupWizard()
        wizard.answer("engagement", "acme")
        wizard.answer_defaults()
        assert "scope" in {s.key for s in wizard.pending()}

    def test_next_step_follows_the_declared_order(self):
        wizard = SetupWizard()
        assert wizard.next_step().key == "engagement"
        wizard.answer("engagement", "acme")
        assert wizard.next_step().key == "scope"

    def test_next_step_is_none_when_nothing_is_left(self):
        wizard = SetupWizard()
        wizard.answer("engagement", "acme")
        wizard.answer("scope", "10.10.0.0/24")
        # The fixpoint fills every remaining default, including the ones unlocked
        # by answering scope (tools_live) and by filling model_url (model_name).
        wizard.answer_defaults()
        assert wizard.next_step() is None


class TestAnswers:
    def test_an_unknown_step_is_refused(self):
        with pytest.raises(KeyError):
            SetupWizard().answer("favourite_colour", "blue")

    def test_values_are_trimmed(self):
        wizard = SetupWizard()
        wizard.answer("engagement", "  acme  ")
        assert wizard.answers["engagement"] == "acme"

    def test_defaults_fill_only_the_steps_that_have_one(self):
        wizard = SetupWizard()
        wizard.answer("engagement", "acme")
        wizard.answer("scope", "10.10.0.0/24")
        wizard.answer_defaults()
        assert wizard.answers["embedder"] == "auto"
        assert wizard.answers["tools_live"] == "0"
        # A step with no default is never invented - only a person may set scope.
        fresh = SetupWizard()
        fresh.answer_defaults()
        assert "scope" not in fresh.answers

    def test_defaults_reach_steps_they_unlock(self):
        """Filling model_url makes model_name askable, so a single pass would
        leave it pending and the wizard looking stuck."""
        wizard = SetupWizard()
        wizard.answer("engagement", "acme")
        wizard.answer_defaults()
        assert wizard.answers["model_url"] == "http://127.0.0.1:11434"
        assert wizard.answers["model_name"] == "llama3.1"

    def test_live_tools_default_to_off(self):
        """The safe default is off. A wizard that turns live execution on for you
        is not a convenience, it is a liability."""
        wizard = SetupWizard()
        wizard.answer("engagement", "acme")
        wizard.answer("scope", "10.10.0.0/24")
        wizard.answer_defaults()
        assert wizard.answers["tools_live"] == "0"

    def test_env_maps_answers_to_their_variables(self):
        wizard = SetupWizard()
        wizard.answer("engagement", "acme")
        wizard.answer("scope", "10.10.0.0/24")
        env = wizard.env()
        assert env["HERMES_ENGAGEMENT"] == "acme"
        assert env["HERMES_SCOPE"] == "10.10.0.0/24"

    def test_unanswered_keys_are_absent_from_env(self):
        """Absent, not empty: a service must be able to tell "not configured"
        from "configured to nothing"."""
        assert SetupWizard().env() == {}

    def test_state_reports_completion(self):
        wizard = SetupWizard()
        assert wizard.state()["complete"] is False
        wizard.answer("engagement", "acme")
        wizard.answer("scope", "10.10.0.0/24")
        wizard.answer_defaults()
        assert wizard.state()["complete"] is True


class TestReadiness:
    def test_a_missing_model_is_reported_not_raised(self):
        readiness = probe_readiness(env={}, which=lambda _n: None, prober=lambda: False)
        assert readiness.model_reachable is False
        assert any("deterministic runner" in n for n in readiness.notes)

    def test_a_reachable_model_is_recorded(self):
        readiness = probe_readiness(env={}, which=lambda _n: "/usr/bin/nmap", prober=lambda: True)
        assert readiness.model_reachable is True
        assert readiness.embedder_available is True

    def test_a_broken_probe_is_treated_as_unreachable(self):
        """A probe that raises must not take setup down with it - the answer is
        "not reachable", which is a state the system already handles."""

        def boom():
            raise OSError("connection reset")

        readiness = probe_readiness(env={}, which=lambda _n: None, prober=boom)
        assert readiness.model_reachable is False
        assert any("model probe failed" in n for n in readiness.notes)

    def test_an_embed_url_alone_marks_the_embedder_available(self):
        readiness = probe_readiness(
            env={"MEMORY_EMBED_URL": "http://127.0.0.1:11434"},
            which=lambda _n: None,
            prober=lambda: False,
        )
        assert readiness.embedder_available is True

    def test_missing_nmap_is_reported(self):
        readiness = probe_readiness(env={}, which=lambda _n: None, prober=lambda: False)
        assert readiness.live_tools_available is False
        assert any("dry-run" in n for n in readiness.notes)

    def test_readiness_is_serialisable_for_the_shell_to_display(self):
        payload = Readiness(model_reachable=True).as_dict()
        assert payload["model_reachable"] is True and isinstance(payload["notes"], list)


class TestWarnings:
    def test_live_tools_without_scope_is_warned_about(self):
        """This is the one combination that produces a machine where every tool
        call is refused, so it must be surfaced before the user finishes."""
        wizard = SetupWizard()
        wizard.answer("engagement", "acme")
        wizard.answer("tools_live", "1")
        assert any("no scope" in w for w in wizard.warnings())

    def test_a_configured_but_dead_model_is_warned_about(self):
        wizard = SetupWizard(readiness=Readiness(model_reachable=False))
        wizard.answer("model_url", "http://127.0.0.1:11434")
        assert any("not reachable" in w for w in wizard.warnings())

    def test_forced_ollama_without_an_endpoint_is_warned_about(self):
        wizard = SetupWizard(readiness=Readiness(model_reachable=False, embedder_available=False))
        wizard.answer("embedder", "ollama")
        assert any("degrade" in w for w in wizard.warnings())

    def test_a_clean_configuration_produces_no_warnings(self):
        wizard = SetupWizard(readiness=Readiness(model_reachable=True, embedder_available=True))
        wizard.answer("engagement", "acme")
        wizard.answer("scope", "10.10.0.0/24")
        wizard.answer("embedder", "auto")
        wizard.answer("model_url", "http://127.0.0.1:11434")
        assert wizard.warnings() == []

    def test_warnings_never_block_completion(self):
        """A warning describes a reduced mode, not a broken system. Refusing to
        finish over an optional feature would be worse than the reduced mode."""
        wizard = SetupWizard(readiness=Readiness())
        wizard.answer("engagement", "acme")
        wizard.answer("tools_live", "1")  # no scope: the one real warning case
        wizard.answer("scope", "10.10.0.0/24")
        wizard.answer_defaults()
        assert wizard.warnings() != []
        assert wizard.state()["complete"] is True


class TestApply:
    def test_apply_refuses_without_confirmation(self):
        """A wizard that reports success while changing nothing is the exact
        failure the module warns about, so this raises instead of returning {}."""
        wizard = SetupWizard()
        wizard.answer("engagement", "acme")
        with pytest.raises(PermissionError, match="confirmed=True"):
            wizard.apply()

    def test_apply_with_confirmation_returns_what_it_would_write(self):
        wizard = SetupWizard()
        wizard.answer("engagement", "acme")
        wizard.answer("scope", "10.10.0.0/24")
        result = wizard.apply(confirmed=True)
        assert result["applied"]["HERMES_SCOPE"] == "10.10.0.0/24"
        assert result["steps"] == 2
