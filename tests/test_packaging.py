"""Phase 4, item 4b: the ISO build pipeline is a real, verifiable build.

The claim under test is not "an ISO exists" - it cannot, and pretending otherwise
would be the worst possible outcome for a build script. The claim is that the
part of the pipeline that *can* run here does run, produces genuine artifacts, and
can be verified from those artifacts alone:

* three real ``.deb`` metapackages, built by ``dpkg-deb``, with a non-empty
  payload and correct control metadata;
* a real chroot overlay tarball containing the services, systemd units, session
  entry and first-boot wizard - and **no** build detritus;
* a manifest recording path, size and sha256 for every artifact, which is then
  checked against the files on disk, so the manifest cannot describe something
  that failed to appear.

The pipeline's boundary is asserted too. ``bootable_iso`` must be false and the
manifest must name what a bootable ISO additionally needs; a build that quietly
implied it had produced an ISO would send someone to boot a file that is not one.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "packaging" / "build.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("dpkg-deb") is None, reason="dpkg-deb is required to build the metapackages"
)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Run the real build once and hand the tests its output directory."""
    out = tmp_path_factory.mktemp("pkgbuild")
    result = subprocess.run(
        ["bash", str(BUILD), "--out", str(out)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert result.returncode == 0, f"build failed:\n{result.stdout}\n{result.stderr}"
    return out, result


@pytest.fixture(scope="module")
def manifest(built):
    out, _ = built
    return json.loads((out / "BUILD-MANIFEST.json").read_text())


# ----------------------------------------------------------- metapackages
class TestMetapackages:
    def test_all_three_debs_are_built(self, built):
        out, _ = built
        debs = sorted(p.name for p in (out / "debs").glob("*.deb"))
        assert debs == [
            "kali-ai-native-agents_0.1.0_all.deb",
            "kali-ai-native-core_0.1.0_all.deb",
            "kali-ai-native-shell_0.1.0_all.deb",
        ]

    @pytest.mark.parametrize(
        "package",
        ["kali-ai-native-core", "kali-ai-native-shell", "kali-ai-native-agents"],
    )
    def test_control_metadata_is_correct(self, built, package):
        out, _ = built
        deb = out / "debs" / f"{package}_0.1.0_all.deb"
        def field(name):
            return subprocess.run(
                ["dpkg-deb", "-f", str(deb), name], capture_output=True, text=True, check=True
            ).stdout.strip()

        assert field("Package") == package
        assert field("Version") == "0.1.0"
        assert field("Architecture") == "all"

    def test_core_depends_on_the_runtime_it_needs(self, built):
        out, _ = built
        depends = subprocess.run(
            ["dpkg-deb", "-f", str(out / "debs" / "kali-ai-native-core_0.1.0_all.deb"), "Depends"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "systemd" in depends
        assert "sqlite3" in depends

    def test_agents_metapackage_depends_on_core(self, built):
        out, _ = built
        depends = subprocess.run(
            ["dpkg-deb", "-f", str(out / "debs" / "kali-ai-native-agents_0.1.0_all.deb"), "Depends"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "kali-ai-native-core" in depends

    @pytest.mark.parametrize(
        "package",
        ["kali-ai-native-core", "kali-ai-native-shell", "kali-ai-native-agents"],
    )
    def test_each_deb_carries_a_real_payload(self, built, package):
        """An empty metapackage would be a metadata-only stub, not an install."""
        out, _ = built
        listing = subprocess.run(
            ["dpkg-deb", "-c", str(out / "debs" / f"{package}_0.1.0_all.deb")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert f"usr/share/doc/{package}/README.md" in listing
        assert f"usr/share/kali-ai/{package}/component.json" in listing

    def test_component_json_declares_the_services_and_safety_defaults(self, built):
        out, _ = built
        deb = out / "debs" / "kali-ai-native-core_0.1.0_all.deb"
        extract = out / "extract-core"
        subprocess.run(["dpkg-deb", "-x", str(deb), str(extract)], check=True)
        payload = json.loads(
            (extract / "usr/share/kali-ai/kali-ai-native-core/component.json").read_text()
        )
        ports = {s["component"]: s["port"] for s in payload["services"]}
        assert ports["kanban-core"] == 8081
        assert ports["memory-store"] == 8087
        assert payload["safety"]["dry_run_default"] is True
        assert payload["safety"]["live_execution_env"] == "TOOLS_LIVE=1"

    def test_shell_component_json_lists_the_new_surfaces(self, built):
        out, _ = built
        extract = out / "extract-shell"
        subprocess.run(
            ["dpkg-deb", "-x", str(out / "debs" / "kali-ai-native-shell_0.1.0_all.deb"), str(extract)],
            check=True,
        )
        payload = json.loads(
            (extract / "usr/share/kali-ai/kali-ai-native-shell/component.json").read_text()
        )
        for surface in ("file_manager_drop", "overlay_widget", "window_manager"):
            assert surface in payload["surfaces"]

    def test_build_is_deterministic_for_the_metapackages(self, built, tmp_path):
        """The same tree must produce the same bytes.

        Determinism is what makes the manifest's sha256 meaningful as a check on
        the *build* rather than just a checksum of whatever happened to be written
        at that moment. ``SOURCE_DATE_EPOCH`` pins the archive timestamps and
        ``--root-owner-group`` pins ownership; without the epoch, two builds of an
        identical tree differ purely because they happened at different times.
        """
        out, _ = built
        again = tmp_path / "again"
        subprocess.run(
            ["bash", str(BUILD), "--out", str(again), "--skip-rootfs"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(REPO),
        )
        for name in (
            "kali-ai-native-core_0.1.0_all.deb",
            "kali-ai-native-shell_0.1.0_all.deb",
            "kali-ai-native-agents_0.1.0_all.deb",
        ):
            first = hashlib.sha256((out / "debs" / name).read_bytes()).hexdigest()
            second = hashlib.sha256((again / "debs" / name).read_bytes()).hexdigest()
            assert first == second, f"{name} is not reproducible"

    def test_the_rootfs_overlay_is_also_reproducible(self, built, tmp_path):
        """The tarball is the biggest artifact, so an unpinned one would make the
        whole manifest unstable on its own."""
        out, _ = built
        again = tmp_path / "again-full"
        subprocess.run(
            ["bash", str(BUILD), "--out", str(again)],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(REPO),
        )
        first = hashlib.sha256((out / "ai-native-kali-rootfs.tar.gz").read_bytes()).hexdigest()
        second = hashlib.sha256((again / "ai-native-kali-rootfs.tar.gz").read_bytes()).hexdigest()
        assert first == second


# ------------------------------------------------------------ chroot overlay
class TestRootfsOverlay:
    def test_rootfs_tarball_is_produced(self, built):
        out, _ = built
        tarball = out / "ai-native-kali-rootfs.tar.gz"
        assert tarball.is_file() and tarball.stat().st_size > 10_000

    def test_rootfs_contains_every_service_component(self, built):
        out, _ = built
        with tarfile.open(out / "ai-native-kali-rootfs.tar.gz") as tf:
            names = tf.getnames()
        for component in (
            "kanban-core",
            "tool-frontends",
            "agent-runtime",
            "observability",
            "hermes-shell",
            "memory-store",
        ):
            assert any(f"./opt/ai-native-kali/{component}/" in n for n in names), component

    def test_rootfs_contains_the_systemd_units_and_target(self, built):
        out, _ = built
        with tarfile.open(out / "ai-native-kali-rootfs.tar.gz") as tf:
            names = set(tf.getnames())
        for unit in (
            "etc/systemd/system/kali-ai.target",
            "etc/systemd/system/kali-ai-kanban.service",
            "etc/systemd/system/kali-ai-observability.service",
            "etc/systemd/system/hermes-shell.service",
        ):
            assert f"./{unit}" in names, unit

    def test_rootfs_contains_the_session_entry_and_wizard(self, built):
        out, _ = built
        with tarfile.open(out / "ai-native-kali-rootfs.tar.gz") as tf:
            names = set(tf.getnames())
        assert "./usr/share/wayland-sessions/hermes-shell.desktop" in names
        assert "./usr/bin/kali-ai-setup" in names

    def test_session_entry_is_executable_and_names_the_session(self, built):
        out, _ = built
        with tarfile.open(out / "ai-native-kali-rootfs.tar.gz") as tf:
            member = tf.getmember("./usr/share/wayland-sessions/hermes-shell.desktop")
            body = tf.extractfile(member).read().decode()
        assert "Exec=/usr/bin/hermes-shell-session" in body
        assert "Hermes AI Desktop" in body

    def test_wizard_is_preserved_executable(self, built):
        """A first-boot wizard that is not executable is a wizard that never runs."""
        out, _ = built
        with tarfile.open(out / "ai-native-kali-rootfs.tar.gz") as tf:
            member = tf.getmember("./usr/bin/kali-ai-setup")
        assert member.mode & 0o111, "kali-ai-setup lost its executable bit"

    def test_rootfs_ships_the_metapackages_for_the_install_hook(self, built):
        out, _ = built
        with tarfile.open(out / "ai-native-kali-rootfs.tar.gz") as tf:
            names = set(tf.getnames())
        for pkg in ("kali-ai-native-core", "kali-ai-native-shell", "kali-ai-native-agents"):
            assert f"./opt/ai-native-kali/metapackages/{pkg}/DEBIAN/control" in names

    def test_rootfs_creates_the_first_boot_state_dirs(self, built):
        out, _ = built
        with tarfile.open(out / "ai-native-kali-rootfs.tar.gz") as tf:
            names = set(tf.getnames())
        assert "./var/lib/kali-ai/models" in names

    def test_rootfs_excludes_build_detritus(self, built):
        """Shipping __pycache__ into an image is a real, common mistake."""
        out, _ = built
        with tarfile.open(out / "ai-native-kali-rootfs.tar.gz") as tf:
            names = tf.getnames()
        offenders = [
            n for n in names if "__pycache__" in n or n.endswith(".pyc") or ".pytest_cache" in n
        ]
        assert offenders == [], offenders


# ---------------------------------------------------------------- manifest
class TestManifest:
    def test_manifest_lists_every_artifact_with_a_path(self, manifest):
        paths = {a["path"] for a in manifest["artifacts"]}
        assert paths == {
            "debs/kali-ai-native-core_0.1.0_all.deb",
            "debs/kali-ai-native-shell_0.1.0_all.deb",
            "debs/kali-ai-native-agents_0.1.0_all.deb",
            "ai-native-kali-rootfs.tar.gz",
        }

    def test_every_manifest_hash_matches_the_file_on_disk(self, built, manifest):
        """A manifest written before the artifacts would be able to lie."""
        out, _ = built
        for artifact in manifest["artifacts"]:
            path = out / artifact["path"]
            assert path.is_file(), artifact["path"]
            assert path.stat().st_size == artifact["bytes"], artifact["path"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest == artifact["sha256"], artifact["path"]

    def test_manifest_is_honest_about_not_being_a_bootable_iso(self, manifest):
        assert manifest["bootable_iso"] is False
        assert manifest["kind"] == "staged-build"
        note = manifest["note"].lower()
        assert "live-build" in note and "xorriso" in note

    def test_manifest_records_the_components_and_metapackages(self, manifest):
        assert "memory-store" in manifest["components"]
        assert manifest["metapackages"] == [
            "kali-ai-native-core",
            "kali-ai-native-shell",
            "kali-ai-native-agents",
        ]


class TestSkipRootfs:
    def test_skip_rootfs_omits_the_artifact_and_says_so(self, tmp_path):
        out = tmp_path / "noboot"
        result = subprocess.run(
            ["bash", str(BUILD), "--out", str(out), "--skip-rootfs"],
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        assert result.returncode == 0, result.stderr
        assert not (out / "ai-native-kali-rootfs.tar.gz").exists()
        manifest = json.loads((out / "BUILD-MANIFEST.json").read_text())
        assert all(a["name"].endswith(".deb") for a in manifest["artifacts"])
        assert manifest["bootable_iso"] is False


class TestBuildScriptContract:
    def test_script_is_syntactically_valid(self):
        result = subprocess.run(["bash", "-n", str(BUILD)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_script_fails_loudly_when_a_required_tool_is_absent(self, tmp_path):
        """A build that skips a missing tool and reports success is worse than one that fails."""
        # A PATH containing the shell but none of the build tools. Overriding PATH
        # with an empty dir would take `bash` with it, which tests nothing about
        # the script's own guards.
        shim = tmp_path / "bin"
        shim.mkdir()
        (shim / "bash").symlink_to(shutil.which("bash"))
        env = dict(os.environ)
        env["PATH"] = str(shim)
        result = subprocess.run(
            [shutil.which("bash"), str(BUILD), "--out", str(tmp_path / "o")],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0
        assert "missing required tool" in result.stderr

    def test_unknown_argument_is_rejected(self):
        result = subprocess.run(
            ["bash", str(BUILD), "--nonsense"], capture_output=True, text=True, cwd=str(REPO)
        )
        assert result.returncode == 2
        assert "unknown argument" in result.stderr

    def test_script_documents_the_boundary_it_cannot_cross(self):
        body = BUILD.read_text()
        assert "live-build" in body
        assert "xorriso" in body
        assert "does not produce a bootable" in body.replace("It ", "").replace("not ", "not ")
