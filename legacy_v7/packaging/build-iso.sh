#!/usr/bin/env bash
# Build the AI-native Kali ISO (Phase 4 scaffold).
#
# Not runnable end to end yet: it needs a Debian/Kali host with live-build and
# root, and the metapackages are still v0.1.0 skeletons. It exists so the
# pipeline in blueprint section 07 is a real script rather than a diagram.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-/tmp/ai-native-kali-build}"

# --- preflight -------------------------------------------------------------
# Report *every* missing requirement at once rather than dying on the first.
# Discovering `mksquashfs` is missing after a 45-minute bootstrap is the way an
# ISO build wastes an afternoon (Phase 8 hit exactly that class of failure).
echo "== preflight"
PREFLIGHT_FAIL=0

if [ "$(id -u)" -eq 0 ]; then
  echo "   ok      root privileges"
else
  echo "   FAIL    root privileges (run under sudo)" >&2
  PREFLIGHT_FAIL=1
fi

for tool in lb xorriso debootstrap mksquashfs; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "   ok      $tool"
  else
    case "$tool" in
      lb)          pkg="live-build" ;;
      xorriso)     pkg="xorriso" ;;
      debootstrap) pkg="debootstrap" ;;
      mksquashfs)  pkg="squashfs-tools" ;;
    esac
    echo "   FAIL    $tool (apt-get install $pkg)" >&2
    PREFLIGHT_FAIL=1
  fi
done

# The chroot needs to create device nodes; a seccomp-restricted container denies
# mknod(2) even with CAP_MKNOD present, and debootstrap then fails deep into the
# bootstrap. Probe it here so the failure is named up front.
PROBE="$(mktemp -u /tmp/.mknod-probe.XXXXXX)"
if mknod "$PROBE" c 1 3 2>/dev/null; then
  rm -f "$PROBE"
  echo "   ok      mknod permitted (chroot device nodes)"
else
  echo "   FAIL    mknod denied by the kernel/seccomp policy - debootstrap cannot" >&2
  echo "           create device nodes. Build on a host that is not a" >&2
  echo "           seccomp-restricted container." >&2
  PREFLIGHT_FAIL=1
fi

if [ "$PREFLIGHT_FAIL" -ne 0 ]; then
  echo "== preflight failed; not starting a build that cannot finish" >&2
  exit 1
fi

echo "== staging $BUILD_DIR"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cp -r "$ROOT/packaging/live-build/auto" "$BUILD_DIR/auto"
cp -r "$ROOT/packaging/live-build/config" "$BUILD_DIR/config"
mkdir -p "$BUILD_DIR/config/includes.chroot/opt/ai-native-kali"
cp -r "$ROOT/packaging/metapackages" "$BUILD_DIR/config/includes.chroot/opt/ai-native-kali/metapackages"
# install -D creates the parent directory; a bare `cp` into usr/bin/ fails on a
# fresh staging tree because nothing has made that directory yet (defect #24).
install -D -m 0755 "$ROOT/packaging/hermes-session/kali-ai-setup" \
  "$BUILD_DIR/config/includes.chroot/usr/bin/kali-ai-setup"

# The AI-native services themselves (source tree) go into the image.
for component in kanban-core tool-frontends agent-runtime observability hermes-shell; do
  mkdir -p "$BUILD_DIR/config/includes.chroot/opt/ai-native-kali/$component"
  cp -r "$ROOT/$component/"* "$BUILD_DIR/config/includes.chroot/opt/ai-native-kali/$component/"
done

# systemd units + session entry
install -D -m 0644 "$ROOT/packaging/hermes-session/kali-ai.target" \
  "$BUILD_DIR/config/includes.chroot/etc/systemd/system/kali-ai.target"
for unit in kali-ai-kanban.service kali-ai-observability.service hermes-shell.service; do
  install -D -m 0644 "$ROOT/packaging/hermes-session/$unit" \
    "$BUILD_DIR/config/includes.chroot/etc/systemd/system/$unit"
done
install -D -m 0644 "$ROOT/packaging/hermes-session/hermes-shell.desktop" \
  "$BUILD_DIR/config/includes.chroot/usr/share/wayland-sessions/hermes-shell.desktop"
# The session the .desktop Exec= line names. Without it the greeter offers a
# Hermes session that immediately dies back to the login screen.
install -D -m 0755 "$ROOT/packaging/hermes-session/hermes-shell-session" \
  "$BUILD_DIR/config/includes.chroot/usr/bin/hermes-shell-session"

# --- offline model bundle (Phase 8, item 3) --------------------------------
# The *config* half is staged here and is deterministic; the weights are staged
# by the build host with packaging/fetch_bundle.py and copied in if present. A
# build without the weights still produces a correct image that reports an empty
# bundle, rather than one that silently falls back and looks fine.
PROFILE="${AI_PROFILE:-minimal}"
BUNDLE_STAGE="${BUNDLE_STAGE:-/var/lib/kali-ai/models}"
echo "== staging model bundle config (profile: $PROFILE)"
mkdir -p "$BUILD_DIR/config/includes.chroot/opt/ai-native-kali/bundle"
python3 "$ROOT/packaging/fetch_bundle.py" manifest --profile "$PROFILE" \
  --out "$BUILD_DIR/config/includes.chroot/opt/ai-native-kali/bundle/manifest.json"
# The tooling travels with the image so a build host can re-verify what it staged.
install -D -m 0644 "$ROOT/packaging/profiles.py" \
  "$BUILD_DIR/config/includes.chroot/opt/ai-native-kali/bundle/profiles.py"
install -D -m 0755 "$ROOT/packaging/fetch_bundle.py" \
  "$BUILD_DIR/config/includes.chroot/opt/ai-native-kali/bundle/fetch_bundle.py"
if [ -d "$BUNDLE_STAGE" ] && [ -n "$(ls -A "$BUNDLE_STAGE" 2>/dev/null)" ]; then
  echo "  -> copying staged weights from $BUNDLE_STAGE"
  mkdir -p "$BUILD_DIR/config/includes.chroot/opt/hermes/models"
  cp -r "$BUNDLE_STAGE/." "$BUILD_DIR/config/includes.chroot/opt/hermes/models/"
else
  echo "  !! no weights staged at $BUNDLE_STAGE - image will report an empty bundle"
fi

cd "$BUILD_DIR"
echo "== lb config"
bash auto/config
echo "== lb build (this is the long one)"
lb build

echo "== done: $(ls -1 "$BUILD_DIR"/*.iso 2>/dev/null || echo 'no iso produced')"
