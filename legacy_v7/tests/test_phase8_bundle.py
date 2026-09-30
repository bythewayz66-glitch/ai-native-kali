"""The offline model bundle's fetch/verify half (Phase 8, item 3).

`profiles.py` describes the bundle; `fetch_bundle.py` stages and verifies it. The
load-bearing checks here are the ones a build host depends on:

* the staging plan is reproducible and names every artifact,
* `verify` reports a *missing* artifact rather than passing,
* `verify` distinguishes "checked and matched" from "not checked" (unpinned),
* a hash mismatch is caught,
* the ISO build script actually wires the bundle in - a bundle nobody stages is a
  bundle that does not ship.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

from fetch_bundle import (  # noqa: E402
    DEFAULT_STAGE_DIR,
    main,
    plan,
    stage_commands,
    verify,
)
from profiles import get_profile, model_bundle  # noqa: E402


class TestStagingPlan:
    def test_the_plan_names_every_artifact_the_profile_ships(self):
        profile = get_profile("workstation")
        result = plan("workstation")
        assert len(result["artifacts"]) == len(profile.models)
        assert {a["name"] for a in result["artifacts"]} == set(profile.models)

    def test_the_plan_pulls_each_model_through_ollama(self):
        # `ollama pull` rather than a raw fetch: the staged layout must be the one
        # the runtime expects, or the bundle verifies and then does not load.
        commands = stage_commands("workstation")
        pulls = [c for c in commands if "ollama pull" in c]
        assert len(pulls) == len(get_profile("workstation").models)
        assert all("OLLAMA_MODELS=" in c for c in pulls)

    def test_the_plan_creates_the_stage_dir_first(self):
        assert stage_commands("gpu")[0].startswith("install -d")

    def test_the_minimal_profile_has_nothing_to_pull(self):
        result = plan("minimal")
        assert result["artifacts"] == []
        assert [c for c in result["commands"] if "ollama pull" in c] == []

    def test_the_plan_is_marked_offline(self):
        # The whole point of bundling: the *image* needs no network.
        assert plan("workstation")["offline"] is True

    def test_the_plan_is_deterministic(self):
        assert plan("gpu") == plan("gpu")

    def test_an_unknown_profile_is_a_loud_error(self):
        with pytest.raises(KeyError):
            plan("toaster")


class TestVerify:
    def test_a_missing_artifact_is_reported_not_passed(self, tmp_path):
        result = verify("workstation", stage_dir=str(tmp_path))
        assert result["ok"] is False
        assert len(result["problems"]) == len(get_profile("workstation").models)
        assert all("missing required artifact" in p for p in result["problems"])

    def test_a_staged_artifact_with_no_pinned_hash_is_unpinned_not_ok(self, tmp_path):
        # "we did not check" and "we checked and it matched" are different facts.
        for model in get_profile("workstation").models:
            (tmp_path / model).write_text("weights", encoding="utf-8")
        result = verify("workstation", stage_dir=str(tmp_path))
        assert result["ok"] is True
        assert all(a["status"] == "unpinned" for a in result["artifacts"])

    def test_a_hash_mismatch_is_caught(self, tmp_path, monkeypatch):
        import fetch_bundle

        model = get_profile("workstation").models[0]
        (tmp_path / model).write_text("tampered", encoding="utf-8")
        # Pin a hash that does not match what is on disk.
        real_bundle = fetch_bundle.model_bundle

        def fake_bundle(name):
            bundle = real_bundle(name)
            for artifact in bundle["artifacts"]:
                artifact["sha256"] = "0" * 64
            return bundle

        monkeypatch.setattr(fetch_bundle, "model_bundle", fake_bundle)
        result = fetch_bundle.verify("workstation", stage_dir=str(tmp_path))
        assert result["ok"] is False
        assert any("sha256 mismatch" in p for p in result["problems"])

    def test_a_matching_hash_passes(self, tmp_path, monkeypatch):
        import fetch_bundle

        model = get_profile("workstation").models[0]
        payload = b"real weights"
        (tmp_path / model).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        real_bundle = fetch_bundle.model_bundle

        def fake_bundle(name):
            bundle = real_bundle(name)
            for artifact in bundle["artifacts"]:
                artifact["sha256"] = digest if artifact["name"] == model else ""
            return bundle

        monkeypatch.setattr(fetch_bundle, "model_bundle", fake_bundle)
        result = fetch_bundle.verify("workstation", stage_dir=str(tmp_path))
        statuses = {a["name"]: a["status"] for a in result["artifacts"]}
        assert statuses[model] == "ok"

    def test_verify_reports_every_problem_at_once(self, tmp_path):
        result = verify("gpu", stage_dir=str(tmp_path))
        assert len(result["problems"]) == len(get_profile("gpu").models)

    def test_the_minimal_profile_verifies_trivially(self, tmp_path):
        # Nothing to stage means nothing to be missing.
        result = verify("minimal", stage_dir=str(tmp_path))
        assert result["ok"] is True
        assert result["artifact_count"] == 0


class TestCli:
    def test_plan_mode_prints_json_and_exits_zero(self, capsys):
        assert main(["plan", "--profile", "workstation"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["profile"] == "workstation"

    def test_manifest_mode_writes_the_same_json_the_image_carries(self, tmp_path, capsys):
        target = tmp_path / "manifest.json"
        assert main(["manifest", "--profile", "gpu", "--out", str(target)]) == 0
        assert json.loads(target.read_text(encoding="utf-8")) == model_bundle("gpu")

    def test_verify_mode_exits_nonzero_on_a_missing_bundle(self, tmp_path):
        assert main(["verify", "--profile", "workstation", "--stage-dir", str(tmp_path)]) == 1

    def test_verify_mode_exits_zero_on_a_complete_bundle(self, tmp_path):
        for model in get_profile("workstation").models:
            (tmp_path / model).write_text("w", encoding="utf-8")
        assert main(["verify", "--profile", "workstation", "--stage-dir", str(tmp_path)]) == 0


class TestIsoWiring:
    def test_the_iso_script_stages_the_bundle_manifest(self):
        script = (ROOT / "packaging" / "build-iso.sh").read_text(encoding="utf-8")
        assert "fetch_bundle.py" in script
        assert "manifest" in script
        assert "AI_PROFILE" in script

    def test_the_iso_script_copies_staged_weights_when_present(self):
        script = (ROOT / "packaging" / "build-iso.sh").read_text(encoding="utf-8")
        assert "BUNDLE_STAGE" in script
        assert "/opt/hermes/models" in script

    def test_the_chroot_hook_writes_the_embedder_env(self):
        hook = (
            ROOT / "packaging" / "live-build" / "config" / "hooks" / "0010-model-bundle.hook.chroot"
        ).read_text(encoding="utf-8")
        assert "MEMORY_EMBEDDER" in hook
        assert "OLLAMA_MODELS" in hook
        assert "model-bundle.env" in hook

    def test_the_default_stage_dir_matches_the_documented_one(self):
        # The doc and the code must not drift; BUILD_HOST.md names this path.
        assert DEFAULT_STAGE_DIR == "/var/lib/kali-ai/models"
