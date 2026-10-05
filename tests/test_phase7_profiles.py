"""Hardware profiles and the offline model bundle (Phase 7, item 4).

The load-bearing checks:

* a profile cannot ship a model bundle it is unable to run (the RAM/disk floor),
* the shipped profiles are all coherent,
* the bundle's embedder configuration agrees with the artifacts it ships - a
  bundle that stages a semantic embedder while configuring the lexical one ships
  bytes nobody uses,
* the preflight reports what is missing instead of raising, because that is what
  a build host has to print.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))

from profiles import (  # noqa: E402
    BUNDLE_ROOT,
    DEFAULT_PROFILE,
    MODEL_SIZE_GB,
    PROFILES,
    HardwareProfile,
    get_profile,
    model_bundle,
    preflight,
    profile_names,
    validate_all,
    write_bundle_manifest,
)


class TestShippedProfiles:
    def test_every_shipped_profile_is_coherent(self):
        assert validate_all() == {}

    def test_the_profile_set_covers_the_machine_classes(self):
        assert set(profile_names()) == {"minimal", "workstation", "gpu"}

    def test_the_default_is_the_smallest_profile(self):
        # A GPU default would fail on most machines, and it would fail at first
        # model use rather than at boot. The default must be the safe one.
        default = get_profile(DEFAULT_PROFILE)
        assert default.models == ()
        assert all(default.minimum_ram_gb <= p.minimum_ram_gb for p in PROFILES)

    def test_the_minimal_profile_ships_no_model_but_still_works(self):
        bundle = model_bundle("minimal")
        assert bundle["artifact_count"] == 0
        # No model -> the lexical backend, stated explicitly rather than left to
        # a default that might later change.
        assert bundle["embedder_env"]["MEMORY_EMBEDDER"] == "hashing"

    def test_a_profile_that_ships_a_model_configures_a_semantic_embedder(self):
        bundle = model_bundle("workstation")
        assert bundle["artifact_count"] >= 2
        assert bundle["embedder_env"]["MEMORY_EMBEDDER"] == "auto"
        assert bundle["embedder_env"]["MEMORY_EMBED_MODEL"]

    def test_an_unknown_profile_is_a_loud_error(self):
        with pytest.raises(KeyError):
            get_profile("toaster")


class TestProfileValidation:
    def test_a_bundle_larger_than_the_ram_floor_is_rejected(self):
        # The defect this exists to catch: an image that cannot run its own model.
        profile = HardwareProfile(
            name="broken",
            label="Broken",
            minimum_ram_gb=4.0,
            recommended_ram_gb=8.0,
            disk_gb=100.0,
            gpu_required=False,
            cpu_hint="any",
            models=("llama3.1:8b-instruct-q4_K_M",),  # 4.9 GB + 2 GB overhead
        )
        problems = profile.validate()
        assert any("exceeds" in p and "minimum_ram_gb" in p for p in problems)

    def test_a_bundle_larger_than_the_disk_budget_is_rejected(self):
        profile = HardwareProfile(
            name="tiny",
            label="Tiny",
            minimum_ram_gb=64.0,
            recommended_ram_gb=64.0,
            disk_gb=1.0,
            gpu_required=False,
            cpu_hint="any",
            models=("llama3.1:8b-instruct-q4_K_M",),
        )
        assert any("disk budget" in p for p in profile.validate())

    def test_an_unlisted_model_is_rejected_rather_than_assumed_to_be_free(self):
        profile = HardwareProfile(
            name="mystery",
            label="Mystery",
            minimum_ram_gb=64.0,
            recommended_ram_gb=64.0,
            disk_gb=200.0,
            gpu_required=False,
            cpu_hint="any",
            models=("some-model-nobody-measured",),
        )
        assert any("unknown model" in p for p in profile.validate())

    def test_a_minimum_above_the_recommendation_is_rejected(self):
        profile = HardwareProfile(
            name="inverted",
            label="Inverted",
            minimum_ram_gb=64.0,
            recommended_ram_gb=16.0,
            disk_gb=100.0,
            gpu_required=False,
            cpu_hint="any",
        )
        assert any("exceeds recommended" in p for p in profile.validate())

    def test_gpu_required_with_nothing_to_accelerate_is_rejected(self):
        profile = HardwareProfile(
            name="empty-gpu",
            label="Empty GPU",
            minimum_ram_gb=32.0,
            recommended_ram_gb=64.0,
            disk_gb=100.0,
            gpu_required=True,
            cpu_hint="any",
        )
        assert any("no models to accelerate" in p for p in profile.validate())

    def test_validate_reports_every_problem_not_just_the_first(self):
        profile = HardwareProfile(
            name="bad",
            label="Bad",
            minimum_ram_gb=2.0,
            recommended_ram_gb=1.0,
            disk_gb=0.5,
            gpu_required=False,
            cpu_hint="any",
            models=("llama3.1:8b-instruct-q4_K_M",),
        )
        assert len(profile.validate()) >= 2


class TestModelBundle:
    def test_the_bundle_states_absolute_paths_inside_the_image(self):
        bundle = model_bundle("workstation")
        assert bundle["root"] == BUNDLE_ROOT
        for artifact in bundle["artifacts"]:
            assert artifact["path"].startswith(BUNDLE_ROOT)
            assert artifact["required"] is True

    def test_the_bundle_is_marked_offline(self):
        # "offline" is the property being claimed: the model path must work with
        # no network, which is the whole reason to bundle rather than pull.
        assert model_bundle("workstation")["offline"] is True

    def test_ollama_is_pointed_at_the_bundle_not_the_network(self):
        env = model_bundle("gpu")["ollama_env"]
        assert env["OLLAMA_MODELS"] == BUNDLE_ROOT
        assert env["OLLAMA_OFFLINE"] == "1"
        assert env["OLLAMA_HOST"].startswith("127.0.0.1")

    def test_the_totals_add_up(self):
        bundle = model_bundle("gpu")
        expected = round(sum(MODEL_SIZE_GB[m] for m in get_profile("gpu").models), 2)
        assert bundle["total_gb"] == expected
        assert len(bundle["artifacts"]) == bundle["artifact_count"]

    def test_the_manifest_written_to_disk_matches_the_in_memory_bundle(self, tmp_path):
        target = tmp_path / "bundle" / "manifest.json"
        write_bundle_manifest("workstation", target)
        assert json.loads(target.read_text(encoding="utf-8")) == model_bundle("workstation")

    def test_an_incoherent_profile_cannot_reach_the_bundle_path(self):
        # `model_bundle` resolves through `get_profile`, so only shipped (valid)
        # profiles are reachable by name.
        with pytest.raises(KeyError):
            model_bundle("no-such-profile")


class TestPreflight:
    def test_preflight_reports_rather_than_raises(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda _name: None)
        result = preflight()
        assert result["ok"] is False
        assert len(result["missing_tools"]) == 3
        assert any("live-build" in m for m in result["missing_tools"])

    def test_preflight_names_root_as_a_requirement(self):
        assert preflight()["needs_root"] is True

    def test_preflight_states_the_blocker_in_words(self):
        # A preflight that fails without saying why is just a slower failure.
        assert "root" in preflight()["note"]
        assert "Kali" in preflight()["note"]

    def test_preflight_reports_profile_problems_when_tools_are_present(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/" + _name)
        result = preflight()
        assert result["profile_problems"] == {}
        assert result["missing_tools"] == []

    def test_on_this_host_the_preflight_reports_the_real_blocker(self):
        """The honest check: the preflight must report *this host's* real state.

        On a host without the ISO toolchain it must say so (``ok`` False, the
        blocker named); on a host that has them it must say ``ok`` True. The
        earlier version asserted a fixed ``False``, which made the test a claim
        about the sandbox rather than about the preflight - so it failed the
        moment the toolchain was actually installed, which is the opposite of
        what a preflight test should do.
        """
        import shutil

        result = preflight()
        tools_present = all(shutil.which(t) for t in ("lb", "xorriso", "debootstrap"))
        assert result["ok"] is (tools_present and not result["profile_problems"])
        # Whatever is missing, it must be named - a preflight that fails without
        # saying why is just a slower failure.
        if not result["ok"]:
            assert result["missing_tools"] or result["profile_problems"]
