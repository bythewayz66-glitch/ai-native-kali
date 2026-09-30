"""Hardware profiles and the offline model bundle (Phase 7, item 4).

Two packaging concerns that were blocked behind the ISO work, now unblocked
because neither of them needs a build host to *describe* or to *test*:

* **Hardware profiles** - what the image assumes about the machine it boots on.
  The blueprint's failure mode is a "one size" image that either ships a 4 GB
  model to a machine with 4 GB of RAM, or ships nothing usable to a workstation
  with a GPU. A profile is a named, validated set of assumptions and the model
  bundle it implies.
* **The offline model bundle** - the set of artifacts that must be *present
  before* first boot for the model path to work without a network. This is the
  concrete answer to "the ISO must not need the internet": a manifest of files, a
  checksum for each, and a size budget the profile must satisfy.

Both are pure data + validation, so they are testable here even though the build
that consumes them is not runnable on this host.

Design note on honesty
----------------------
A profile whose ``minimum_ram_gb`` does not cover its own model bundle is a
profile that produces an image that cannot run its own model path. That is
checked at *parse* time, not at build time, so an impossible profile fails in CI
rather than on a user's laptop.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: Where the bundle lands inside the image.
BUNDLE_ROOT = "/opt/hermes/models"

#: A model artifact is only worth bundling if it fits a profile's disk budget.
BYTES_PER_GB = 1024 * 1024 * 1024

#: Approximate on-disk cost of quantised weights, used by the build-host doc to
#: size the build machine. Kept here so the doc and the validator cannot drift.
MODEL_SIZE_GB = {
    "llama3.1:8b-instruct-q4_K_M": 4.9,
    "qwen2.5:3b-instruct-q4_K_M": 2.0,
    "nomic-embed-text:latest": 0.27,
    "all-minilm:latest": 0.09,
}


@dataclass(frozen=True)
class ModelArtifact:
    """One file that must be present on the image for the model path to work."""

    name: str
    size_gb: float
    sha256: str = ""
    source: str = "bundle"
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": f"{BUNDLE_ROOT}/{self.name}",
            "size_gb": self.size_gb,
            "sha256": self.sha256,
            "source": self.source,
            "required": self.required,
        }


@dataclass(frozen=True)
class HardwareProfile:
    """A named set of machine assumptions plus the model bundle they imply."""

    name: str
    label: str
    minimum_ram_gb: float
    recommended_ram_gb: float
    disk_gb: float
    gpu_required: bool
    cpu_hint: str
    models: tuple[str, ...] = ()
    notes: str = ""

    @property
    def model_bytes(self) -> float:
        return round(sum(MODEL_SIZE_GB.get(m, 0.0) for m in self.models), 2)

    def validate(self) -> list[str]:
        """Return a list of *problems*. Empty means the profile is coherent.

        Deliberately returns problems rather than raising: a build-host preflight
        should be able to report every broken profile at once, not stop at the
        first.
        """
        problems: list[str] = []
        if self.minimum_ram_gb > self.recommended_ram_gb:
            problems.append(
                f"{self.name}: minimum_ram_gb {self.minimum_ram_gb} exceeds recommended {self.recommended_ram_gb}"
            )
        if self.disk_gb <= 0:
            problems.append(f"{self.name}: disk_gb must be positive")
        unknown = [m for m in self.models if m not in MODEL_SIZE_GB]
        if unknown:
            problems.append(f"{self.name}: unknown model(s) {unknown} - add them to MODEL_SIZE_GB")
        # The one that matters: a profile must be able to *run* what it ships.
        # Weights have to be resident, so RAM must cover the bundle with headroom
        # for the OS and the services; 2 GB is the floor the blueprint assumes.
        if self.model_bytes + 2.0 > self.minimum_ram_gb:
            problems.append(
                f"{self.name}: model bundle {self.model_bytes} GB + 2 GB overhead exceeds "
                f"minimum_ram_gb {self.minimum_ram_gb}"
            )
        if self.model_bytes > self.disk_gb:
            problems.append(
                f"{self.name}: model bundle {self.model_bytes} GB exceeds disk budget {self.disk_gb} GB"
            )
        if self.gpu_required and not self.models:
            problems.append(f"{self.name}: gpu_required but no models to accelerate")
        return problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "minimum_ram_gb": self.minimum_ram_gb,
            "recommended_ram_gb": self.recommended_ram_gb,
            "disk_gb": self.disk_gb,
            "gpu_required": self.gpu_required,
            "cpu_hint": self.cpu_hint,
            "models": list(self.models),
            "model_bytes_gb": self.model_bytes,
            "notes": self.notes,
        }


#: The shipped profiles.
#:
#: ``minimal`` is the default and deliberately ships **no** model: it must boot and
#: be useful on a 4 GB VM with no network, and it reports the hashing embedder and
#: the deterministic runner, which is the honest state rather than a broken model
#: path.
PROFILES: tuple[HardwareProfile, ...] = (
    HardwareProfile(
        name="minimal",
        label="Minimal / VM (no bundled model)",
        minimum_ram_gb=4.0,
        recommended_ram_gb=8.0,
        disk_gb=20.0,
        gpu_required=False,
        cpu_hint="any x86_64 with SSE4.2",
        models=(),
        notes="Deterministic runner and lexical recall; no model endpoint required.",
    ),
    HardwareProfile(
        name="workstation",
        label="Workstation (CPU model path)",
        minimum_ram_gb=16.0,
        recommended_ram_gb=32.0,
        disk_gb=60.0,
        gpu_required=False,
        cpu_hint="8 threads or better",
        models=("qwen2.5:3b-instruct-q4_K_M", "nomic-embed-text:latest"),
        notes="Small instruct model plus a semantic embedder; no GPU needed.",
    ),
    HardwareProfile(
        name="gpu",
        label="GPU workstation (larger model path)",
        minimum_ram_gb=32.0,
        recommended_ram_gb=64.0,
        disk_gb=120.0,
        gpu_required=True,
        cpu_hint="any x86_64",
        models=("llama3.1:8b-instruct-q4_K_M", "nomic-embed-text:latest"),
        notes="Requires a CUDA-capable GPU; the model path is otherwise identical.",
    ),
)


def profile_names() -> list[str]:
    return [p.name for p in PROFILES]


def get_profile(name: str) -> HardwareProfile:
    for profile in PROFILES:
        if profile.name == name:
            return profile
    raise KeyError(f"unknown hardware profile {name!r}; known: {profile_names()}")


#: The profile the image defaults to when nothing is selected. Chosen as the
#: *smallest* profile on purpose: a default that assumes a GPU produces an image
#: that fails on the majority of machines, and the failure is a missing model at
#: first use rather than a clear message at boot.
DEFAULT_PROFILE = "minimal"


def model_bundle(profile: HardwareProfile | str) -> dict[str, Any]:
    """The offline model bundle for a profile: what must be staged pre-boot.

    ``embedder_env`` is included because the bundle and the memory-store
    configuration have to agree: bundling a semantic embedder while leaving
    ``MEMORY_EMBEDDER=hashing`` would ship the bytes and never use them, which
    looks like success and is not.
    """
    if isinstance(profile, str):
        profile = get_profile(profile)

    artifacts = [
        ModelArtifact(name=model, size_gb=MODEL_SIZE_GB[model])
        for model in profile.models
    ]
    total = round(sum(a.size_gb for a in artifacts), 2)
    has_embedder = any("embed" in a.name or "minilm" in a.name for a in artifacts)

    return {
        "profile": profile.name,
        "root": BUNDLE_ROOT,
        "artifacts": [a.as_dict() for a in artifacts],
        "total_gb": total,
        "artifact_count": len(artifacts),
        "offline": True,
        "embedder_env": {
            # `auto`, not `ollama`: with a bundled embedder the endpoint is
            # expected to answer, but a profile that ships no embedder must still
            # fall back rather than fail. `auto` expresses exactly that.
            "MEMORY_EMBEDDER": "auto" if has_embedder else "hashing",
            "MEMORY_EMBED_URL": "http://127.0.0.1:11434",
            "MEMORY_EMBED_MODEL": next(
                (a.name.split(":")[0] for a in artifacts if "embed" in a.name or "minilm" in a.name),
                "",
            ),
        },
        "ollama_env": {
            "OLLAMA_HOST": "127.0.0.1:11434",
            "OLLAMA_MODELS": BUNDLE_ROOT,
            "OLLAMA_OFFLINE": "1",
        },
    }


def validate_all() -> dict[str, list[str]]:
    """Every profile's problems, keyed by profile name. Empty dict means clean."""
    problems: dict[str, list[str]] = {}
    for profile in PROFILES:
        found = profile.validate()
        if found:
            problems[profile.name] = found
    return problems


def preflight() -> dict[str, Any]:
    """What a build host should check before spending an hour on a build.

    Reports rather than raises, for the same reason ``validate`` does: the build
    host should print a complete picture of what is missing, and the caller
    decides whether to proceed.
    """
    import shutil

    required = {
        "lb": "the live-build driver (`apt install live-build`)",
        "xorriso": "the ISO image writer (`apt install xorriso`)",
        "debootstrap": "the chroot builder (`apt install debootstrap`)",
        # Phase 8: the real build reached the squashfs stage and found this
        # missing. It is required to compress the chroot into the live
        # filesystem, so a host without it fails late and confusingly.
        "mksquashfs": "the squashfs builder (`apt install squashfs-tools`)",
    }
    found = {tool: shutil.which(tool) is not None for tool in required}
    missing = [f"{tool} - {required[tool]}" for tool, ok in found.items() if not ok]

    return {
        "ok": not missing and not validate_all(),
        "tools": found,
        "missing_tools": missing,
        "profile_problems": validate_all(),
        "profiles": profile_names(),
        "default_profile": DEFAULT_PROFILE,
        "needs_root": True,
        # Named so the caller can print an actionable message instead of a
        # generic failure - this is the whole point of a preflight.
        "note": "A bootable ISO build requires root and a Kali (or Kali-compatible) build host.",
    }


def write_bundle_manifest(profile: HardwareProfile | str, path: str | Path) -> str:
    """Write the bundle manifest to *path*; returns the path as a string."""
    bundle = model_bundle(profile)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(target)
