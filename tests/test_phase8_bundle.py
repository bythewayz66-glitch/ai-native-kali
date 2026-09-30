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


def _stage_flat_pinned(root, monkeypatch, contents: bytes = b"weights"):
    """Stage flat files and pin their *true* digests so ``verify`` can pass.

    The shipped models now carry real registry digests, so a test that writes
    arbitrary bytes to a flat file would (correctly) fail the hash check. This
    helper pins the digest of exactly what it wrote, which is what a build host
    that staged real weights would have.
    """
    import fetch_bundle

    real_bundle = fetch_bundle.model_bundle
    digests = {}
    for model in get_profile("workstation").models:
        (root / model).write_bytes(contents)
        digests[model] = hashlib.sha256(contents).hexdigest()

    def fake_bundle(name):
        bundle = real_bundle(name)
        for artifact in bundle["artifacts"]:
            artifact["sha256"] = digests.get(artifact["name"], "")
        return bundle

    monkeypatch.setattr(fetch_bundle, "model_bundle", fake_bundle)


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

    def test_a_staged_artifact_with_no_pinned_hash_is_unpinned_not_ok(self, tmp_path, monkeypatch):
        # "we did not check" and "we checked and it matched" are different facts.
        # The bundle is monkeypatched to carry no pinned digest, which is the
        # only way to reach the ``unpinned`` state now that the shipped models
        # have real digests pinned.
        import fetch_bundle

        real_bundle = fetch_bundle.model_bundle

        def unpinned_bundle(name):
            bundle = real_bundle(name)
            for artifact in bundle["artifacts"]:
                artifact["sha256"] = ""
            return bundle

        monkeypatch.setattr(fetch_bundle, "model_bundle", unpinned_bundle)
        for model in get_profile("workstation").models:
            (tmp_path / model).write_text("weights", encoding="utf-8")
        result = fetch_bundle.verify("workstation", stage_dir=str(tmp_path))
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

    def test_verify_mode_exits_zero_on_a_complete_bundle(self, tmp_path, monkeypatch):
        _stage_flat_pinned(tmp_path, monkeypatch)
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


class TestRealOllamaLayout:
    """The layout ``ollama pull`` actually writes.

    Regression for a real defect: ``verify`` originally looked for a file named
    after the model (``nomic-embed-text:latest``). ``ollama pull`` writes no such
    file - it writes a manifest under ``manifests/registry.ollama.ai/library``
    and a content-addressed blob under ``blobs/`` - so a *correctly staged*
    bundle was reported as **missing**. The synthetic-bundle tests above never
    caught it because they staged the flat layout the verifier expected. These
    tests stage the real layout instead.
    """

    def _stage_ollama(self, root, name, payload):
        """Write *name* the way ``ollama pull`` does, returning its digest."""
        model, _, tag = name.partition(":")
        tag = tag or "latest"
        digest = hashlib.sha256(payload).hexdigest()
        manifest = root / "manifests" / "registry.ollama.ai" / "library" / model / tag
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('{"schemaVersion":2}', encoding="utf-8")
        blobs = root / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        (blobs / f"sha256-{digest}").write_bytes(payload)
        return digest

    def test_a_real_ollama_layout_verifies_ok(self, tmp_path, monkeypatch):
        import fetch_bundle

        model = get_profile("workstation").models[0]
        digest = self._stage_ollama(tmp_path, model, b"real weights")
        real_bundle = fetch_bundle.model_bundle

        def fake_bundle(name):
            bundle = real_bundle(name)
            for artifact in bundle["artifacts"]:
                artifact["sha256"] = digest if artifact["name"] == model else ""
            return bundle

        monkeypatch.setattr(fetch_bundle, "model_bundle", fake_bundle)
        result = fetch_bundle.verify("workstation", stage_dir=str(tmp_path))
        entry = next(a for a in result["artifacts"] if a["name"] == model)
        assert entry["present"] is True
        assert entry["layout"] == "ollama"
        assert entry["status"] == "ok"

    def test_a_real_layout_with_a_tampered_blob_is_a_mismatch(self, tmp_path, monkeypatch):
        import fetch_bundle

        model = get_profile("workstation").models[0]
        self._stage_ollama(tmp_path, model, b"real weights")
        real_bundle = fetch_bundle.model_bundle

        def fake_bundle(name):
            bundle = real_bundle(name)
            for artifact in bundle["artifacts"]:
                artifact["sha256"] = "0" * 64
            return bundle

        monkeypatch.setattr(fetch_bundle, "model_bundle", fake_bundle)
        result = fetch_bundle.verify("workstation", stage_dir=str(tmp_path))
        # The blob is named by its true digest, so a wrong pin finds no blob at
        # all - which is a *missing* artifact, not a silent pass.
        assert result["ok"] is False

    def test_the_flat_layout_still_verifies(self, tmp_path, monkeypatch):
        # Backwards compatibility: a build host that staged flat files must not
        # start failing because the real layout was added.
        _stage_flat_pinned(tmp_path, monkeypatch)
        result = verify("workstation", stage_dir=str(tmp_path))
        assert result["ok"] is True
        assert all(a["layout"] == "flat" for a in result["artifacts"])


class TestPinnedDigests:
    """The digests that make "checked and matched" possible at all."""

    def test_the_embedder_digest_is_pinned(self):
        from profiles import MODEL_SHA256

        assert MODEL_SHA256["nomic-embed-text:latest"] == (
            "970aa74c0a90ef7482477cf803618e776e173c007bf957f635f1015bfcfef0e6"
        )

    def test_every_pinned_digest_is_a_well_formed_sha256(self):
        from profiles import MODEL_SHA256

        for name, digest in MODEL_SHA256.items():
            assert len(digest) == 64, name
            assert all(c in "0123456789abcdef" for c in digest), name

    def test_the_bundle_carries_the_pinned_digest_through(self):
        bundle = model_bundle("workstation")
        embedder = next(a for a in bundle["artifacts"] if "nomic" in a["name"])
        assert embedder["sha256"] == (
            "970aa74c0a90ef7482477cf803618e776e173c007bf957f635f1015bfcfef0e6"
        )
