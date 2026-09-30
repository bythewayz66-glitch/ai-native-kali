#!/usr/bin/env python3
"""Verify a staged build against its own manifest.

``make verify-build`` - answers one question honestly: *is the directory in front
of me the build the manifest describes?* Every artifact is checked three ways:
it exists, its size matches, and its sha256 matches.

This is deliberately not a "does the build pass" script. A build that produced
nothing would pass a test that only ran the builder; a manifest whose hashes are
never checked is documentation, not verification. So this reads the manifest and
then *distrusts it*, re-hashing the files.

Exit codes:
  0  every artifact present and hashing correctly
  1  at least one mismatch, or the manifest is missing/unreadable
  2  a required artifact is absent entirely
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1] if len(argv) > 1 else "var/build")
    manifest_path = out_dir / "BUILD-MANIFEST.json"

    if not manifest_path.is_file():
        print(f"FAIL: no manifest at {manifest_path}", file=sys.stderr)
        print("      run `make build` first", file=sys.stderr)
        return 1

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"FAIL: manifest is not valid JSON: {exc}", file=sys.stderr)
        return 1

    artifacts = manifest.get("artifacts") or []
    if not artifacts:
        print("FAIL: manifest lists no artifacts", file=sys.stderr)
        return 1

    print(f"verifying {len(artifacts)} artifact(s) in {out_dir}")
    print(f"  project={manifest.get('project')} version={manifest.get('version')} "
          f"kind={manifest.get('kind')}")

    missing = 0
    mismatched = 0
    total_bytes = 0

    for artifact in artifacts:
        rel = artifact.get("path") or artifact.get("name")
        path = out_dir / rel
        if not path.is_file():
            print(f"  MISSING  {rel}")
            missing += 1
            continue

        size = path.stat().st_size
        total_bytes += size
        digest = sha256(path)

        size_ok = size == artifact.get("bytes")
        hash_ok = digest == artifact.get("sha256")

        if size_ok and hash_ok:
            print(f"  ok       {rel} ({size} bytes, {digest[:16]}...)")
            continue

        mismatched += 1
        if not size_ok:
            print(f"  SIZE     {rel}: on disk {size}, manifest {artifact.get('bytes')}")
        if not hash_ok:
            print(f"  HASH     {rel}: on disk {digest[:16]}..., manifest {str(artifact.get('sha256'))[:16]}...")

    # A manifest that is honest about not being a bootable ISO is part of the
    # contract, so it is verified here too rather than only in the test suite.
    if manifest.get("bootable_iso") is not False:
        print("  WARN     manifest does not declare bootable_iso=false", file=sys.stderr)

    print(f"total {total_bytes} bytes")
    if missing:
        print(f"FAIL: {missing} artifact(s) absent", file=sys.stderr)
        return 2
    if mismatched:
        print(f"FAIL: {mismatched} artifact(s) do not match the manifest", file=sys.stderr)
        return 1

    print("OK: every artifact present and hashing correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
