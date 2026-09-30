"""Launcher and application-registry tests (Phase 7, item 1).

Two things are being pinned here, and they are different claims:

1. **The registry is a real catalogue** - it parses ``.desktop`` files, dedupes
   against the fallback so a shipped entry wins, carries a tool's guardrail tier
   through, and searches in the stated three-tier order.
2. **A launch is a managed window** - not a side channel. The contract that
   matters is that ``Launcher.launch`` routes through
   ``WindowManager.apply("open", ...)``, so the resulting window is in the same
   z-order / focus / taskbar model everything else uses. That is asserted by
   observing the state machine, not by trusting the call site.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_shell.launcher import (
    AppRegistry,
    LaunchEntry,
    Launcher,
    build_registry,
    entries_from_tool_specs,
    load_desktop_entries,
    parse_desktop_entry,
)
from hermes_shell.wm import WindowManager

DESKTOP_SAMPLE = """[Desktop Entry]
Type=Application
Name=Sample Editor
Comment=Edits things
Exec=/usr/bin/sample --new
Icon=sample
Categories=Utility;TextEditor;
Keywords=edit;write;text;
"""


def fake_spec(name: str, *, tier: int = 0, requires_scope: bool = False, category: str = "network"):
    return SimpleNamespace(
        name=name,
        binary=f"{name}-bin",
        category=category,
        tier=tier,
        description=f"{name} description",
        intent_examples=["scan a host", "enumerate ports"],
        requires_scope=requires_scope,
    )


class TestParseDesktopEntry:
    def test_parses_the_fields_the_launcher_uses(self):
        entry = parse_desktop_entry(DESKTOP_SAMPLE)
        assert entry is not None
        assert entry.name == "Sample Editor"
        assert entry.exec == "/usr/bin/sample --new"
        assert entry.comment == "Edits things"
        assert entry.icon == "sample"
        assert entry.categories == ["Utility", "TextEditor"]
        assert entry.keywords == ["edit", "write", "text"]
        assert entry.kind == "app"

    def test_hidden_entries_are_not_offered(self):
        text = "[Desktop Entry]\nName=Hidden Thing\nHidden=true\nExec=/bin/true\n"
        assert parse_desktop_entry(text) is None

    def test_nodisplay_entries_are_not_offered(self):
        text = "[Desktop Entry]\nName=NoDisplay Thing\nNoDisplay=true\nExec=/bin/true\n"
        assert parse_desktop_entry(text) is None

    def test_missing_desktop_entry_group_is_rejected(self):
        assert parse_desktop_entry("[Desktop Action Foo]\nName=Nope\n") is None

    def test_entry_without_a_name_is_rejected(self):
        assert parse_desktop_entry("[Desktop Entry]\nExec=/bin/true\n") is None

    def test_only_the_first_group_is_read(self):
        # A second group's Name must not leak into the launcher entry.
        text = DESKTOP_SAMPLE + "\n[Desktop Action Other]\nName=Other Action\nExec=/bin/other\n"
        entry = parse_desktop_entry(text)
        assert entry is not None and entry.name == "Sample Editor"


class TestLoadDesktopEntries:
    def test_reads_desktop_files_and_skips_missing_directories(self, tmp_path):
        apps = tmp_path / "applications"
        apps.mkdir()
        (apps / "sample.desktop").write_text(DESKTOP_SAMPLE, encoding="utf-8")
        (apps / "notes.txt").write_text("not a desktop file", encoding="utf-8")

        entries = load_desktop_entries([str(apps), str(tmp_path / "does-not-exist")])
        assert [e.name for e in entries] == ["Sample Editor"]

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert load_desktop_entries([str(tmp_path / "nope")]) == []


class TestToolEntries:
    def test_a_tool_spec_becomes_a_launch_entry_with_its_tier(self):
        entries = entries_from_tool_specs([fake_spec("nmap_scan", tier=1, requires_scope=True)])
        assert len(entries) == 1
        entry = entries[0]
        assert entry.id == "tool.nmap_scan"
        assert entry.kind == "tool"
        assert entry.tier == 1
        assert entry.requires_scope is True
        assert entry.tier_label == "low impact"
        assert "scan a host" in entry.keywords

    def test_tier_labels_are_ordered_by_severity(self):
        specs = [fake_spec("a", tier=0), fake_spec("b", tier=1), fake_spec("c", tier=2), fake_spec("d", tier=3)]
        labels = [e.tier_label for e in entries_from_tool_specs(specs)]
        assert labels == ["read-only", "low impact", "intrusive", "high impact"]

    def test_a_spec_without_a_name_is_skipped(self):
        bad = SimpleNamespace(binary="x", category="net", tier=0, description="", intent_examples=[])
        assert entries_from_tool_specs([bad]) == []


class TestRegistry:
    def test_first_write_wins_so_a_shipped_entry_beats_the_fallback(self):
        registry = AppRegistry()
        shipped = LaunchEntry(id="org.kali.hermes.terminal", name="Terminal (packaged)")
        registry.add(shipped)
        registry.add(LaunchEntry(id="org.kali.hermes.terminal", name="Terminal (fallback)"))
        assert registry.get("org.kali.hermes.terminal").name == "Terminal (packaged)"

    def test_entries_are_sorted_by_kind_then_name(self):
        registry = AppRegistry(
            [
                LaunchEntry(id="t.2", name="zulu", kind="tool"),
                LaunchEntry(id="a.1", name="alpha", kind="app"),
                LaunchEntry(id="s.1", name="session", kind="session"),
                LaunchEntry(id="t.1", name="alpha", kind="tool"),
            ]
        )
        assert [e.id for e in registry.entries()] == ["a.1", "t.1", "t.2", "s.1"]

    def test_counts_groups_by_kind(self):
        registry = AppRegistry(
            [
                LaunchEntry(id="a", name="a", kind="app"),
                LaunchEntry(id="t", name="t", kind="tool"),
                LaunchEntry(id="t2", name="t2", kind="tool"),
            ]
        )
        assert registry.counts() == {"app": 1, "tool": 2}

    def test_build_registry_includes_the_session_entry(self):
        registry = build_registry(desktop_dirs=[])
        entry = registry.get("org.kali.hermes.session")
        assert entry is not None
        assert entry.exec == "/usr/bin/hermes-shell-session"

    def test_build_registry_includes_fallback_apps_and_tools(self):
        registry = build_registry(
            desktop_dirs=[], tool_specs=[fake_spec("nmap_scan", tier=1)]
        )
        assert registry.get("org.kali.hermes.files") is not None
        assert registry.get("tool.nmap_scan") is not None
        assert registry.counts()["session"] == 1

    def test_a_shipped_desktop_file_overrides_the_fallback(self, tmp_path):
        apps = tmp_path / "applications"
        apps.mkdir()
        (apps / "org.kali.hermes.files.desktop").write_text(
            "[Desktop Entry]\nName=Files (packaged)\nExec=/usr/bin/hermes-files\n", encoding="utf-8"
        )
        registry = build_registry(desktop_dirs=[str(apps)], include_fallback=True)
        assert registry.get("org.kali.hermes.files").name == "Files (packaged)"

    def test_fallback_can_be_disabled(self):
        registry = build_registry(desktop_dirs=[], include_fallback=False)
        assert registry.get("org.kali.hermes.terminal") is None


class TestSearch:
    @pytest.fixture()
    def registry(self):
        return build_registry(
            desktop_dirs=[],
            tool_specs=[
                fake_spec("nmap_scan", tier=1),
                fake_spec("nmap_nse_index", tier=0),
                fake_spec("smb_enum", tier=1),
            ],
        )

    def test_empty_query_returns_everything(self, registry):
        assert len(registry.search("")) == len(registry)

    def test_exact_name_ranks_before_a_longer_relative(self, registry):
        results = registry.search("nmap_scan")
        assert results[0].id == "tool.nmap_scan"

    def test_shorter_name_wins_within_a_tier(self, registry):
        results = registry.search("nmap")
        assert [r.id for r in results[:2]] == ["tool.nmap_scan", "tool.nmap_nse_index"]

    def test_search_is_case_insensitive(self, registry):
        assert registry.search("NMAP")[0].id == "tool.nmap_scan"

    def test_keyword_match_finds_a_tool_by_intent(self, registry):
        # "enumerate ports" is a keyword, not part of the name or id.
        assert any(r.id == "tool.nmap_scan" for r in registry.search("enumerate"))

    def test_category_is_searchable(self, registry):
        assert any(r.kind == "tool" for r in registry.search("network"))

    def test_kind_filter_excludes_other_kinds(self, registry):
        results = registry.search("hermes", kind="session")
        assert all(r.kind == "session" for r in results)

    def test_limit_is_respected(self, registry):
        assert len(registry.search("", limit=3)) == 3

    def test_a_non_match_returns_nothing(self, registry):
        assert registry.search("zzz-no-such-entry") == []


class TestLaunch:
    @pytest.fixture()
    def launcher(self):
        registry = build_registry(desktop_dirs=[], tool_specs=[fake_spec("nmap_scan", tier=1)])
        return Launcher(registry, WindowManager())

    def test_launch_opens_a_managed_window(self, launcher):
        result = launcher.launch("tool.nmap_scan")
        view = result["view"]
        assert result["reused"] is False
        assert result["window_id"] == view["focused"]
        # A managed window: present in the z-order, the taskbar, and focusable.
        assert result["window_id"] in view["stack"]
        assert [w["id"] for w in view["taskbar"]] == [result["window_id"]]

    def test_launch_routes_through_the_window_manager_state_machine(self, launcher):
        seen: list[tuple[str, dict]] = []
        real_apply = launcher.wm.apply

        def spy(action, payload=None):
            seen.append((action, dict(payload or {})))
            return real_apply(action, payload)

        launcher.wm.apply = spy  # type: ignore[method-assign]
        launcher.launch("org.kali.hermes.terminal")

        assert seen and seen[0][0] == "open"
        assert seen[0][1]["app"] == "org.kali.hermes.terminal"
        assert seen[0][1]["title"] == "Terminal"

    def test_window_title_is_the_entry_name(self, launcher):
        result = launcher.launch("tool.nmap_scan")
        window = launcher.wm.get(result["window_id"])
        assert window is not None and window.title == "nmap_scan"

    def test_relaunching_focuses_instead_of_duplicating(self, launcher):
        first = launcher.launch("tool.nmap_scan")
        second = launcher.launch("tool.nmap_scan")
        assert second["reused"] is True
        assert second["window_id"] == first["window_id"]
        assert len(launcher.wm.snapshot()["windows"]) == 1
        assert launcher.launches == 1

    def test_a_closed_window_is_reopened_rather_than_reused(self, launcher):
        first = launcher.launch("tool.nmap_scan")
        launcher.wm.apply("close", {"id": first["window_id"]})
        second = launcher.launch("tool.nmap_scan")
        assert second["reused"] is False
        assert second["window_id"] != first["window_id"]

    def test_launching_a_second_app_puts_it_on_top(self, launcher):
        launcher.launch("org.kali.hermes.terminal")
        second = launcher.launch("tool.nmap_scan")
        assert launcher.wm.snapshot()["stack"][-1] == second["window_id"]

    def test_unknown_entry_is_refused_with_a_key_error(self, launcher):
        with pytest.raises(KeyError):
            launcher.launch("no.such.entry")
        assert launcher.refusals == 1

    def test_every_registry_entry_is_launchable(self, launcher):
        for entry in launcher.registry.entries():
            result = launcher.launch(entry.id)
            assert result["window_id"]
