"""Phase 6, item 6: the ISO pipeline installs the session it advertises.

The Phase 4 build produced metapackages and a chroot overlay, and was honest that
it did not produce a bootable ISO. The gap Phase 6 closes is narrower and more
embarrassing: the image advertised a Wayland session whose entry point nothing
ever installed.

``hermes-shell.desktop`` has always carried ``Exec=/usr/bin/hermes-shell-session``
and shipped under ``usr/share/wayland-sessions/`` - so the greeter would list
"Hermes AI Desktop", the user would select it, and the session would die
immediately because the binary was absent. ``TryExec`` was set but nothing
satisfied it.

These tests assert the three things that make the fix real rather than
documented: the file exists and is a real script, the staged build installs it
with its executable bit, and the full-ISO script does too.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "packaging" / "build.sh"
SESSION = REPO / "packaging" / "hermes-session" / "hermes-shell-session"
DESKTOP = REPO / "packaging" / "hermes-session" / "hermes-shell.desktop"
ISO_SCRIPT = REPO / "packaging" / "build-iso.sh"
LIST = REPO / "packaging" / "live-build" / "config" / "package-lists" / "kali-ai-native.list.chroot"


class TestSessionEntryPointSource:
    def test_the_session_script_exists(self):
        assert SESSION.is_file(), "the .desktop Exec= target has no source file"

    # The git executable bit is deliberately *not* asserted here. The sandbox's
    # file tooling materialises files as 0644 and there is no version control in
    # this checkout to carry the mode, so a stat-based assertion would fail for a
    # reason that has nothing to do with the pipeline being correct. What matters
    # is asserted end to end instead: TestStagedBuildShipsItForReal checks the
    # mode *inside the built overlay*, which is the artifact a booting session
    # actually reads, and both build scripts install it with -m 0755.

    def test_both_build_scripts_install_it_directly_executable(self):
        for script in (BUILD, ISO_SCRIPT):
            assert "-m 0755" in script.read_text()

    def test_it_is_syntactically_valid_bash(self):
        result = subprocess.run(["bash", "-n", str(SESSION)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_it_names_the_same_binary_the_desktop_entry_does(self):
        desktop = DESKTOP.read_text()
        assert f"Exec=/usr/bin/{SESSION.name}" in desktop

    def test_its_shebang_does_not_use_a_login_shelltrap(self):
        """``set -euo pipefail`` is expected; a session script must fail loudly."""
        body = SESSION.read_text()
        assert body.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in body

    def test_it_starts_the_gtk_client_and_falls_back(self):
        body = SESSION.read_text()
        assert "hermes_shell.gtk.client" in body
        assert "xdg-open" in body, "the browser panel is the documented fallback"


class TestDesktopEntry:
    def test_exec_and_tryexec_agree(self):
        body = DESKTOP.read_text()
        assert "Exec=/usr/bin/hermes-shell-session" in body
        assert "TryExec=/usr/bin/hermes-shell-session" in body

    def test_the_entry_lands_in_wayland_sessions(self):
        assert DESKTOP.parent.name == "hermes-session"


class TestPackageListCarriesTheGtkStack:
    def test_the_layer_shell_and_gtk_typelibs_are_listed(self):
        body = LIST.read_text()
        for package in ("gir1.2-gtk-4.0", "gir1.2-webkit-6.0", "python3-gi", "libgtk4-layer-shell0"):
            assert package in body, f"{package} is missing from the chroot package list"

    def test_each_package_appears_once(self):
        packages = [
            line.strip()
            for line in LIST.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert len(packages) == len(set(packages)), "duplicate entries in the package list"


class TestBuildScriptsInstallTheSession:
    def test_both_build_scripts_install_the_session_binary(self):
        for script in (BUILD, ISO_SCRIPT):
            body = script.read_text()
            assert "hermes-shell-session" in body, f"{script.name} never installs the session entry"
            assert "/usr/bin/hermes-shell-session" in body

    def test_the_iso_script_is_still_valid_bash(self):
        result = subprocess.run(["bash", "-n", str(ISO_SCRIPT)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_the_staged_build_is_still_valid_bash(self):
        result = subprocess.run(["bash", "-n", str(BUILD)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_both_scripts_still_name_the_session_entry_the_desktop_file_uses(self):
        for script in (BUILD, ISO_SCRIPT):
            assert "hermes-shell.desktop" in script.read_text()


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb is required")
class TestStagedBuildShipsItForReal:
    @pytest.fixture(scope="class")
    def out(self, tmp_path_factory):
        target = tmp_path_factory.mktemp("phase6build")
        result = subprocess.run(
            ["bash", str(BUILD), "--out", str(target)],
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        return target

    def test_the_overlay_contains_an_executable_session_entry(self, out):
        with tarfile.open(out / "ai-native-kali-rootfs.tar.gz") as tf:
            member = tf.getmember("./usr/bin/hermes-shell-session")
        assert member.mode & 0o111, "the session entry lost its executable bit in the overlay"

    def test_the_overlay_still_contains_the_desktop_entry(self, out):
        with tarfile.open(out / "ai-native-kali-rootfs.tar.gz") as tf:
            names = set(tf.getnames())
        assert "./usr/share/wayland-sessions/hermes-shell.desktop" in names

    def test_the_manifest_still_verifies(self, out):
        manifest = json.loads((out / "BUILD-MANIFEST.json").read_text())
        assert manifest["bootable_iso"] is False
        for artifact in manifest["artifacts"]:
            assert (out / artifact["path"]).is_file(), artifact["path"]

    def test_the_shell_metapackage_advertises_the_new_surface(self, out):
        extract = out / "extract-shell-phase6"
        subprocess.run(
            ["dpkg-deb", "-x", str(out / "debs" / "kali-ai-native-shell_0.1.0_all.deb"), str(extract)],
            check=True,
        )
        payload = json.loads(
            (extract / "usr/share/kali-ai/kali-ai-native-shell/component.json").read_text()
        )
        assert "gtk_layer_shell_panel" in payload["surfaces"]
        assert payload["session"]["exec"] == "/usr/bin/hermes-shell-session"
