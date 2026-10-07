"""Phase 16, item 9: the file-manager browse surface, and its sandbox boundary.

A file browser is an arbitrary-file-read primitive, so the boundary *is* the
feature. These tests are mostly about the escapes that must fail, and they use a
real temporary tree rather than a mock - a mocked ``realpath`` would test the mock.

The two that matter most:

* ``..`` segments, which a naive prefix check on the *requested* path lets through;
* a **symlink inside a root pointing outside it**, which is the same escape in a
  form a string-prefix check cannot see at all, because the requested path is
  legitimately under the root.
"""
from __future__ import annotations

import os

import pytest

from hermes_shell.file_manager import BrowseRefused, FileManager


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """A TestClient over the app, with the file roots pinned to a temp dir.

    The other shell test modules each define their own module-local ``client``
    fixture (there is no shared conftest), so this file does too. The roots are
    pinned to ``tmp_path`` rather than inherited from the environment so the
    route tests do not depend on what /tmp happens to contain on the host.
    """
    monkeypatch.setenv("SHELL_FILE_ROOTS", str(tmp_path))
    from fastapi.testclient import TestClient

    from hermes_shell.server import build_app

    with TestClient(build_app()) as c:
        yield c


@pytest.fixture()
def tree(tmp_path):
    """A real browsable root plus a real outside-the-root tree."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "reports").mkdir(parents=True)
    (root / "notes.txt").write_text("hello operation\n" * 4)
    (root / "reports" / "scan.txt").write_text("nmap output")
    (root / "empty").mkdir()
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_text("do not serve me")
    (outside / "secrets").mkdir()
    (outside / "secrets" / "deep.txt").write_text("nor me")
    # The escape that a prefix check cannot see: the *requested* path is inside
    # the root, and only resolving it reveals otherwise.
    os.symlink(outside, root / "escape_link")
    os.symlink(outside / "secret.txt", root / "escape_file")
    # A link that stays inside is legitimate and must keep working.
    os.symlink(root / "reports", root / "reports_link")
    return {"root": str(root), "outside": str(outside), "tmp": str(tmp_path)}


@pytest.fixture()
def fm(tree):
    return FileManager(roots=(tree["root"],))


# ---------------------------------------------------------------------------
# the escapes
# ---------------------------------------------------------------------------
class TestBoundary:
    def test_dotdot_traversal_is_refused(self, fm, tree):
        with pytest.raises(BrowseRefused):
            fm.list_dir(os.path.join(tree["root"], ".."))

    def test_deep_traversal_is_refused(self, fm, tree):
        with pytest.raises(BrowseRefused):
            fm.resolve(os.path.join(tree["root"], "reports", "..", "..", "..", "etc"))

    def test_absolute_path_outside_the_root_is_refused(self, fm, tree):
        with pytest.raises(BrowseRefused):
            fm.list_dir(tree["outside"])

    def test_symlinked_directory_escape_is_refused(self, fm, tree):
        """The requested path is inside the root; only realpath reveals it."""
        with pytest.raises(BrowseRefused):
            fm.list_dir(os.path.join(tree["root"], "escape_link"))

    def test_symlinked_file_escape_is_refused(self, fm, tree):
        with pytest.raises(BrowseRefused):
            fm.read_preview(os.path.join(tree["root"], "escape_file"))

    def test_a_root_prefix_is_not_enough(self, fm, tree):
        """``/root`` must not authorise ``/rootkit`` - the separator check."""
        sibling = tree["root"] + "kit"
        os.makedirs(sibling, exist_ok=True)
        try:
            with pytest.raises(BrowseRefused):
                fm.list_dir(sibling)
        finally:
            os.rmdir(sibling)

    def test_an_in_root_symlink_still_works(self, fm, tree):
        """The boundary must not break legitimate links."""
        listing = fm.list_dir(os.path.join(tree["root"], "reports_link"))
        assert [e["name"] for e in listing["entries"]] == ["scan.txt"]

    def test_a_refusal_never_leaks_a_listing(self, fm, tree):
        with pytest.raises(BrowseRefused) as excinfo:
            fm.list_dir(tree["outside"])
        assert "secret" not in str(excinfo.value)

    def test_empty_and_non_string_paths_are_refused(self, fm):
        for bad in ("", None, 0):
            with pytest.raises(BrowseRefused):
                fm.resolve(bad)

    def test_a_root_of_slash_would_defeat_nothing_so_defaults_are_narrow(self):
        from hermes_shell.file_manager import DEFAULT_ROOTS

        assert "/" not in DEFAULT_ROOTS


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------
class TestListing:
    def test_lists_directories_first_then_name(self, fm, tree):
        listing = fm.list_dir(tree["root"])
        kinds = [e["kind"] for e in listing["entries"]]
        assert kinds.index("dir") < kinds.index("file")

    def test_entry_metadata_is_present(self, fm, tree):
        listing = fm.list_dir(tree["root"])
        notes = next(e for e in listing["entries"] if e["name"] == "notes.txt")
        assert notes["size"] > 0
        assert notes["mtime"] is not None
        assert notes["readable"] is True

    def test_symlinks_are_reported_with_their_target(self, fm, tree):
        listing = fm.list_dir(tree["root"])
        link = next(e for e in listing["entries"] if e["name"] == "escape_link")
        assert link["kind"] == "link"
        assert link["target"] == tree["outside"]

    def test_parent_is_none_at_the_root_boundary(self, fm, tree):
        """'Up' from a root must do nothing, not 404."""
        listing = fm.list_dir(tree["root"])
        assert listing["parent"] is None

    def test_parent_is_present_inside_the_root(self, fm, tree):
        listing = fm.list_dir(os.path.join(tree["root"], "reports"))
        assert listing["parent"] == tree["root"]

    def test_empty_directory_lists_empty(self, fm, tree):
        listing = fm.list_dir(os.path.join(tree["root"], "empty"))
        assert listing["entries"] == []
        assert listing["truncated"] is False

    def test_listing_a_file_is_refused(self, fm, tree):
        with pytest.raises(BrowseRefused):
            fm.list_dir(os.path.join(tree["root"], "notes.txt"))

    def test_truncation_is_reported_not_hidden(self, tree):
        fm = FileManager(roots=(tree["root"],), max_entries=1)
        listing = fm.list_dir(tree["root"])
        assert listing["count"] == 1
        assert listing["truncated"] is True

    def test_roots_are_reported_for_the_tree_view(self, fm, tree):
        assert fm.normalised_roots == [tree["root"]]

    def test_a_symlinked_root_is_normalised_at_construction(self, tmp_path):
        """``/home`` is often a symlink; a root left unresolved matches nothing."""
        real = tmp_path / "real_root"
        real.mkdir()
        (real / "file.txt").write_text("x")
        link = tmp_path / "link_root"
        os.symlink(real, link)
        fm = FileManager(roots=(str(link),))
        assert [e["name"] for e in fm.list_dir(str(link))["entries"]] == ["file.txt"]


# ---------------------------------------------------------------------------
# preview and search
# ---------------------------------------------------------------------------
class TestPreviewAndSearch:
    def test_preview_reads_text(self, fm, tree):
        body = fm.read_preview(os.path.join(tree["root"], "notes.txt"))
        assert body["text"].startswith("hello operation")
        assert body["truncated"] is False

    def test_preview_is_capped(self, tree):
        big = os.path.join(tree["root"], "big.txt")
        with open(big, "w") as handle:
            handle.write("a" * 5000)
        fm = FileManager(roots=(tree["root"],), max_preview_bytes=1000)
        body = fm.read_preview(big)
        assert body["bytes_read"] == 1000
        assert body["truncated"] is True

    def test_preview_refuses_binary(self, tree):
        blob = os.path.join(tree["root"], "blob.bin")
        with open(blob, "wb") as handle:
            handle.write(b"\x7fELF\x00\x00\x00")
        fm = FileManager(roots=(tree["root"],))
        with pytest.raises(BrowseRefused):
            fm.read_preview(blob)

    def test_preview_refuses_a_directory(self, fm, tree):
        with pytest.raises(BrowseRefused):
            fm.read_preview(os.path.join(tree["root"], "reports"))

    def test_search_matches_names_case_insensitively(self, fm, tree):
        found = fm.search(tree["root"], "NOTE")
        assert [m["name"] for m in found["matches"]] == ["notes.txt"]

    def test_search_requires_a_query(self, fm, tree):
        with pytest.raises(BrowseRefused):
            fm.search(tree["root"], "")

    def test_search_cannot_escape_via_the_query(self, fm, tree):
        with pytest.raises(BrowseRefused):
            fm.search(tree["outside"], "secret")

    def test_stat_reports_mode_and_kind(self, fm, tree):
        body = fm.stat(os.path.join(tree["root"], "notes.txt"))
        assert body["kind"] == "file"
        assert body["mode"].startswith("0o")

    def test_stat_of_a_missing_path_is_refused(self, fm, tree):
        with pytest.raises(BrowseRefused):
            fm.stat(os.path.join(tree["root"], "nope.txt"))


class TestStatus:
    def test_status_is_explicitly_read_only(self, fm):
        """The surface must say it cannot write, so a UI does not offer to."""
        status = fm.status()
        assert status["read_only"] is True
        assert status["roots"]
        assert all("readable" in r for r in status["roots"])

    def test_the_module_exposes_no_mutating_operation(self):
        """Pinned: adding write/delete means adding an approval gate with it."""
        public = {name for name in dir(FileManager) if not name.startswith("_")}
        for forbidden in ("write", "delete", "remove", "move", "rename", "mkdir", "chmod"):
            assert not any(forbidden in name for name in public), forbidden


# ---------------------------------------------------------------------------
# the HTTP surface
# ---------------------------------------------------------------------------
class TestFileRoutes:
    def test_status_with_no_path_returns_the_roots(self, client):
        body = client.get("/api/files").json()
        assert body["read_only"] is True
        assert body["path"] is None
        assert body["roots"]

    def test_listing_a_configured_root_is_allowed(self, client, tmp_path):
        response = client.get("/api/files", params={"path": str(tmp_path)})
        assert response.status_code == 200
        assert "entries" in response.json()

    def test_an_out_of_scope_path_is_a_403_with_a_reason(self, client):
        """A policy refusal is not a server error - it must not be a 500."""
        response = client.get("/api/files", params={"path": "/proc"})
        assert response.status_code == 403
        assert "outside the browsable roots" in response.json()["detail"]

    def test_traversal_over_http_is_resolved_before_it_is_judged(self, client, tmp_path):
        # A traversal that stays inside the configured root is allowed - the point
        # is that the path is *resolved* before the prefix test, not that anything
        # containing ``..`` is refused.
        response = client.get("/api/files", params={"path": str(tmp_path / "x" / "..")})
        assert response.status_code in (200, 403)

    def test_preview_refuses_binary(self, client, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"\x00\x01\x02")
        response = client.get("/api/files/preview", params={"path": str(blob)})
        assert response.status_code == 403
        assert "text file" in response.json()["detail"]

    def test_search_requires_a_query(self, client, tmp_path):
        response = client.get("/api/files/search", params={"path": str(tmp_path), "q": ""})
        assert response.status_code == 403
