#!/usr/bin/env python3
"""Fetch and verify the offline model bundle for a hardware profile.

Phase 8, item 3. `profiles.py` *describes* the bundle (what must be present, how
big it is, which embedder it implies). This script is the other half: the thing a
build host actually runs to stage those artifacts and to check that what it
staged is what the manifest says.

Three modes, and the split is deliberate:

* ``plan``    - print the exact commands that would stage the bundle. No network,
                no side effects. This is what a build host reads before it spends
                an hour downloading weights.
* ``manifest``- write the bundle manifest to a path (the same JSON the image
                carries, so the image and the build host agree by construction).
* ``verify``  - check a staged bundle directory against the manifest: every
                required artifact present, and its sha256 matched when one is
                pinned. Reports rather than raises, so a build host sees the whole
                picture at once.

**Honesty about this sandbox.** This host has no ``ollama`` and no route to a
model registry, so ``plan`` is the only mode that can run here, and it is the
mode that matters for the build host: it is the reproducible instruction set. The
``verify`` path is exercised against a synthetic staged bundle in the tests, which
is the strongest check available without the real weights. Nothing here claims a
model was downloaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from profiles import (  # noqa: E402
    BUNDLE_ROOT,
    MODEL_SIZE_GB,
    get_profile,
    model_bundle,
    write_bundle_manifest,
)

#: Where a build host stages the bundle before it is copied into the image.
DEFAULT_STAGE_DIR = "/var/lib/kali-ai/models"

#: The registry the artifacts come from. Named so the plan is reproducible and so
#: an air-gapped host knows exactly what it must mirror.
DEFAULT_REGISTRY = "https://registry.ollama.ai"


def stage_commands(profile_name: str, *, stage_dir: str = DEFAULT_STAGE_DIR) -> list[str]:
    """The exact commands that stage *profile_name*'s bundle.

    ``ollama pull`` is used rather than a raw registry fetch because it is what
    the image's own runtime uses, so the staged layout is the layout the runtime
    expects - a hand-rolled download that lands the blobs somewhere else is a
    bundle that verifies and then does not load.
    """
    profile = get_profile(profile_name)
    commands = [f"install -d -m 0700 {stage_dir}"]
    for model in profile.models:
        commands.append(f"OLLAMA_MODELS={stage_dir} ollama pull {model}")
    return commands


def plan(profile_name: str, *, stage_dir: str = DEFAULT_STAGE_DIR) -> dict[str, Any]:
    """A machine-readable staging plan: commands, artifacts, and the size budget."""
    bundle = model_bundle(profile_name)
    return {
        "profile": profile_name,
        "stage_dir": stage_dir,
        "registry": DEFAULT_REGISTRY,
        "commands": stage_commands(profile_name, stage_dir=stage_dir),
        "artifacts": bundle["artifacts"],
        "total_gb": bundle["total_gb"],
        "offline": True,
        "note": (
            "Run these on a networked host, then copy the staged tree into the "
            f"image at {BUNDLE_ROOT}. The image itself needs no network."
        ),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ollama_paths(root: Path, name: str) -> tuple[Path, Path]:
    """The manifest and weights-blob paths ollama uses for *name* under *root*.

    ``ollama pull`` does not write a file called ``nomic-embed-text:latest``. It
    writes a manifest at
    ``manifests/registry.ollama.ai/library/<model>/<tag>`` and the weights as a
    content-addressed blob at ``blobs/sha256-<digest>``. A verifier that looks
    for the model *name* therefore reports a perfectly good bundle as missing -
    which is exactly what this function exists to stop. The blob is named by its
    own digest, so the digest is also the path: a staged blob whose name matches
    the pinned sha256 is self-verifying.
    """
    model, _, tag = name.partition(":")
    tag = tag or "latest"
    manifest = root / "manifests" / "registry.ollama.ai" / "library" / model / tag
    return manifest, root / "blobs"


def verify(
    profile_name: str,
    *,
    stage_dir: str = DEFAULT_STAGE_DIR,
    check_hashes: bool = True,
) -> dict[str, Any]:
    """Check a staged bundle against the manifest. Reports, never raises.

    Returns ``{"ok": bool, "problems": [...], "artifacts": [...]}``. A missing
    artifact is a problem; an artifact present but with no pinned sha256 is
    reported as ``unpinned`` rather than passed silently, because "we did not
    check" and "we checked and it matched" are different facts and a build host
    should be able to tell them apart.

    Two layouts are accepted, because a build host may stage either way:

    * **ollama's own layout** (what ``ollama pull`` produces, and what the image
      runtime expects): a manifest under ``manifests/registry.ollama.ai/library``
      plus a content-addressed blob under ``blobs/``. This is the layout the
      staging plan in :func:`plan` actually creates, so it is the one that
      matters.
    * **a flat file named after the model** - the synthetic layout the tests use,
      kept so the verifier can be exercised without a real pull.
    """
    bundle = model_bundle(profile_name)
    root = Path(stage_dir)
    problems: list[str] = []
    checked: list[dict[str, Any]] = []

    for artifact in bundle["artifacts"]:
        name = artifact["name"]
        pinned = artifact.get("sha256") or ""
        manifest, blobs_dir = _ollama_paths(root, name)
        flat = root / name
        flat_alt = root / name.replace(":", "_")

        # Prefer the real layout; fall back to a flat file for synthetic bundles.
        blob = blobs_dir / f"sha256-{pinned}" if pinned else None
        if manifest.exists() and blob is not None and blob.exists():
            present, candidate, layout = True, blob, "ollama"
        elif flat.is_file():
            present, candidate, layout = True, flat, "flat"
        elif flat_alt.is_file():
            present, candidate, layout = True, flat_alt, "flat"
        else:
            present, candidate, layout = False, None, "none"

        entry: dict[str, Any] = {
            "name": name,
            "present": present,
            "sha256": pinned,
            "layout": layout,
        }
        if not present:
            if artifact.get("required", True):
                problems.append(
                    f"missing required artifact: {name} "
                    f"(expected {manifest} + blob under {blobs_dir}, or a file named {name})"
                )
            entry["status"] = "missing"
        elif not pinned:
            entry["status"] = "unpinned"
        elif not check_hashes:
            entry["status"] = "present"
        else:
            actual = _sha256(candidate) if candidate is not None and candidate.is_file() else ""
            if actual and actual != pinned:
                problems.append(f"sha256 mismatch for {name}: manifest {pinned} != staged {actual}")
                entry["status"] = "mismatch"
            else:
                entry["status"] = "ok"
        checked.append(entry)

    return {
        "profile": profile_name,
        "stage_dir": str(root),
        "ok": not problems,
        "problems": problems,
        "artifacts": checked,
        "artifact_count": len(checked),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch/verify the offline model bundle.")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_plan = sub.add_parser("plan", help="print the staging commands (no side effects)")
    p_plan.add_argument("--profile", required=True)
    p_plan.add_argument("--stage-dir", default=DEFAULT_STAGE_DIR)

    p_man = sub.add_parser("manifest", help="write the bundle manifest to a path")
    p_man.add_argument("--profile", required=True)
    p_man.add_argument("--out", required=True)

    p_ver = sub.add_parser("verify", help="verify a staged bundle against the manifest")
    p_ver.add_argument("--profile", required=True)
    p_ver.add_argument("--stage-dir", default=DEFAULT_STAGE_DIR)
    p_ver.add_argument("--no-hashes", action="store_true")

    args = parser.parse_args(argv)

    if args.mode == "plan":
        print(json.dumps(plan(args.profile, stage_dir=args.stage_dir), indent=2))
        return 0
    if args.mode == "manifest":
        path = write_bundle_manifest(args.profile, args.out)
        print(path)
        return 0
    if args.mode == "verify":
        result = verify(args.profile, stage_dir=args.stage_dir, check_hashes=not args.no_hashes)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
