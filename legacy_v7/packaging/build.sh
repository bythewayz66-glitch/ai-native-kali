#!/usr/bin/env bash
# =============================================================================
# AI-native Kali - runnable build pipeline
# =============================================================================
# Produces the *staged* half of the ISO pipeline, for real, on any Debian-like
# host with `dpkg-deb` and `tar` - no root, no live-build, no network.
#
# Two artifacts come out, and they are the two things the ISO builder actually
# consumes:
#
#   1. metapackages/*.deb   - the three metapackages, built with dpkg-deb.
#   2. ai-native-kali-rootfs.tar.gz
#                           - the chroot overlay: services, systemd units, the
#                             session entry, the first-boot wizard.
#   3. BUILD-MANIFEST.json  - sizes + sha256 for everything above, so a build is
#                             reproducible and can be verified after the fact.
#
# What this does NOT do, and why
# ------------------------------
# It does not produce a bootable .iso. That step needs `live-build` plus
# `xorriso`, which are Debian/Kali host packages and are deliberately not
# installed here. Shipping a "build script" that silently produced something
# else and called it an ISO would be the worst outcome: the pipeline would look
# green while the deliverable did not exist.
#
# So the boundary is explicit. Everything up to and including the chroot overlay
# is real and verified; the final `lb build` is a separate, clearly-labelled
# target (`make iso-full`) that fails loudly and helpfully if the host lacks the
# tooling. See docs/BUILD_PIPELINE.md.
#
# Usage:
#   bash packaging/build.sh [--out DIR] [--skip-rootfs]
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${AI_BUILD_OUT:-$ROOT/var/build}"
SKIP_ROOTFS=0

# Reproducible builds. `dpkg-deb` and `tar` both embed wall-clock timestamps by
# default, so two builds of an identical tree used to produce different bytes -
# which makes the manifest's sha256 a checksum of *when* you built rather than of
# *what* you built. Pinning the epoch makes the hashes comparable across runs, and
# that is what lets `make verify-build` mean anything.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1700000000}"

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    --skip-rootfs) SKIP_ROOTFS=1; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

METAPACKAGES="kali-ai-native-core kali-ai-native-shell kali-ai-native-agents"
COMPONENTS="kanban-core tool-frontends agent-runtime observability hermes-shell memory-store"
VERSION="0.1.0"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing required tool: $1" >&2; exit 1; }
}
need dpkg-deb
need tar
need sha256sum
# Phase 6 item 6: the check must follow the capability. Historically `iso-full`
# could "succeed" on a host with no ISO tool, because only the staged half was
# gated. xorriso is what actually writes the bootable image, so its absence is a
# hard failure of the path that needs it.
if [ "${AI_REQUIRE_XORRISO:-0}" = "1" ]; then
  need xorriso
fi

log() { printf '[build] %s\n' "$*"; }

mkdir -p "$OUT_DIR"
STAGE="$OUT_DIR/stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"

# -----------------------------------------------------------------------------
# 1. Metapackages
# -----------------------------------------------------------------------------
DEB_DIR="$OUT_DIR/debs"
rm -rf "$DEB_DIR"
mkdir -p "$DEB_DIR"

for pkg in $METAPACKAGES; do
  src="$ROOT/packaging/metapackages/$pkg"
  if [ ! -f "$src/DEBIAN/control" ]; then
    echo "missing control file for $pkg: $src/DEBIAN/control" >&2
    exit 1
  fi
  log "building $pkg"
  # --root-owner-group keeps ownership deterministic, so two builds of the same
  # tree hash identically and the manifest means something.
  dpkg-deb --build --root-owner-group "$src" "$DEB_DIR/${pkg}_${VERSION}_all.deb" >/dev/null
done

# Verify what we just built by asking dpkg rather than trusting the exit code.
for pkg in $METAPACKAGES; do
  deb="$DEB_DIR/${pkg}_${VERSION}_all.deb"
  [ -s "$deb" ] || { echo "dpkg-deb produced no output for $pkg" >&2; exit 1; }
  name="$(dpkg-deb -f "$deb" Package)"
  [ "$name" = "$pkg" ] || { echo "package name mismatch: $name != $pkg" >&2; exit 1; }
  log "  ok: $name $(dpkg-deb -f "$deb" Version) ($(stat -c%s "$deb") bytes)"
done

# -----------------------------------------------------------------------------
# 2. Chroot overlay
# -----------------------------------------------------------------------------
# This is the tree live-build would drop into config/includes.chroot/. Building
# it here means the *contents* of the image are verifiable without an ISO tool.
if [ "$SKIP_ROOTFS" -eq 0 ]; then
  ROOTFS="$STAGE/rootfs"
  mkdir -p "$ROOTFS"

  log "staging chroot overlay"
  for component in $COMPONENTS; do
    src="$ROOT/$component"
    [ -d "$src" ] || { echo "missing component dir: $src" >&2; exit 1; }
    dest="$ROOTFS/opt/ai-native-kali/$component"
    mkdir -p "$dest"
    # Exclude build/test detritus so the image does not ship caches.
    tar -C "$src" \
      --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
      --exclude='node_modules' --exclude='.git' --exclude='var' \
      -cf - . | tar -C "$dest" -xf -
  done

  # systemd units and the session entry
  install -d "$ROOTFS/etc/systemd/system"
  for unit in "$ROOT"/packaging/hermes-session/*.service "$ROOT"/packaging/hermes-session/kali-ai.target; do
    install -m 0644 "$unit" "$ROOTFS/etc/systemd/system/$(basename "$unit")"
  done
  install -D -m 0644 "$ROOT/packaging/hermes-session/hermes-shell.desktop" \
    "$ROOTFS/usr/share/wayland-sessions/hermes-shell.desktop"
  install -D -m 0755 "$ROOT/packaging/hermes-session/kali-ai-setup" \
    "$ROOTFS/usr/bin/kali-ai-setup"
  # The session entry point the .desktop file Exec= line names. Until Phase 6
  # nothing in the image ever created this binary, so selecting the Hermes
  # session fell back to the greeter with no error.
  install -D -m 0755 "$ROOT/packaging/hermes-session/hermes-shell-session" \
    "$ROOTFS/usr/bin/hermes-shell-session"

  # The metapackages, for the install hook to consume inside the chroot.
  install -d "$ROOTFS/opt/ai-native-kali/metapackages"
  for pkg in $METAPACKAGES; do
    cp -r "$ROOT/packaging/metapackages/$pkg" "$ROOTFS/opt/ai-native-kali/metapackages/$pkg"
  done

  # First-boot state dirs (blueprint section 07)
  install -d -m 0755 "$ROOTFS/var/lib/kali-ai"
  install -d -m 0700 "$ROOTFS/var/lib/kali-ai/models"

  ROOTFS_TAR="$OUT_DIR/ai-native-kali-rootfs.tar.gz"
  rm -f "$ROOTFS_TAR"
  log "packing rootfs overlay"
  # --sort/--mtime/--owner/--group: a stable entry order and no host-specific or
  # time-specific metadata, so the overlay is byte-identical across builds.
  tar -C "$ROOTFS" \
    --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
    -czf "$ROOTFS_TAR" .
  log "  rootfs: $(stat -c%s "$ROOTFS_TAR") bytes, $(tar -tzf "$ROOTFS_TAR" | wc -l) entries"
fi

# -----------------------------------------------------------------------------
# 3. Manifest
# -----------------------------------------------------------------------------
# Written last, over what is actually on disk - not over what we intended to
# write - so the manifest cannot describe an artifact that failed to appear.
log "writing manifest"
MANIFEST="$OUT_DIR/BUILD-MANIFEST.json"
{
  printf '{\n'
  printf '  "project": "ai-native-kali",\n'
  printf '  "version": "%s",\n' "$VERSION"
  printf '  "kind": "staged-build",\n'
  printf '  "bootable_iso": false,\n'
  printf '  "source_date_epoch": %s,\n' "$SOURCE_DATE_EPOCH"
  printf '  "artifact_source": "%s",\n' "$OUT_DIR"
  printf '  "components": [%s],\n' "$(printf '"%s",' $COMPONENTS | sed 's/,$//')"
  printf '  "metapackages": [%s],\n' "$(printf '"%s",' $METAPACKAGES | sed 's/,$//')"
  printf '  "artifacts": [\n'
  first=1
  # Each entry carries its path *relative to the output directory*, not just a
  # basename: the debs live in debs/ and the rootfs at the top level, so a
  # basename-only manifest cannot be verified by a reader who has the output
  # directory and nothing else.
  for file in "$DEB_DIR"/*.deb $( [ "$SKIP_ROOTFS" -eq 0 ] && echo "$OUT_DIR/ai-native-kali-rootfs.tar.gz" ); do
    [ -f "$file" ] || continue
    [ "$first" -eq 1 ] || printf ',\n'
    first=0
    rel="${file#"$OUT_DIR"/}"
    printf '    {"name": "%s", "path": "%s", "bytes": %s, "sha256": "%s"}' \
      "$(basename "$file")" "$rel" "$(stat -c%s "$file")" "$(sha256sum "$file" | cut -d' ' -f1)"
  done
  printf '\n  ],\n'
  printf '  "note": "Staged build: rootfs overlay + metapackages. A bootable ISO additionally requires live-build and xorriso; run `make iso-full` on a Debian/Kali host."\n'
  printf '}\n'
} > "$MANIFEST"

if command -v python3 >/dev/null 2>&1; then
  python3 -c "import json,sys; json.load(open('$MANIFEST')); print('[build] manifest valid JSON')"
fi

log "done -> $OUT_DIR"
ls -la "$OUT_DIR" | sed 's/^/[build]   /'
