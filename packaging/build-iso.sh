#!/usr/bin/env bash
# Build the AI-native Kali ISO (Phase 4 scaffold).
#
# Not runnable end to end yet: it needs a Debian/Kali host with live-build and
# root, and the metapackages are still v0.1.0 skeletons. It exists so the
# pipeline in blueprint section 07 is a real script rather than a diagram.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-/tmp/ai-native-kali-build}"

if ! command -v lb >/dev/null 2>&1; then
  echo "live-build ('lb') is not installed: apt-get install live-build" >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "building an ISO requires root" >&2
  exit 1
fi

echo "== staging $BUILD_DIR"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cp -r "$ROOT/packaging/live-build/auto" "$BUILD_DIR/auto"
cp -r "$ROOT/packaging/live-build/config" "$BUILD_DIR/config"
mkdir -p "$BUILD_DIR/config/includes.chroot/opt/ai-native-kali"
cp -r "$ROOT/packaging/metapackages" "$BUILD_DIR/config/includes.chroot/opt/ai-native-kali/metapackages"
# `install -D` creates the parent directory; a bare `cp` into usr/bin fails
# because only opt/ai-native-kali exists at this point. That failure aborted the
# whole build before lb ever ran.
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
echo "== lb bootstrap"
lb bootstrap
# In a container mknod is denied, so debootstrap cannot create real device
# nodes: chroot/dev/null ends up missing and any postinst that opens /dev/null
# fails -- e.g. the GVM/OpenVAS gpg key import aborts with
# "gpg: Fatal: failed to open '/dev/null': Permission denied", which then
# cascades to ospd-openvas/gvmd/gsad/gvm and fails the whole chroot stage.
# live-build mounts /proc, /sys and /dev/pts but never /dev. A whole-/dev bind
# does not survive lb's mount handling (verified: chroot/dev/null was still
# absent), so bind the individual device nodes the postinsts actually open.
# Harmless on a normal host, where the nodes already exist.
if [ -d "$BUILD_DIR/chroot/dev" ]; then
  for dev in null zero full random urandom tty; do
    if [ -e "/dev/$dev" ]; then
      touch "$BUILD_DIR/chroot/dev/$dev" 2>/dev/null || true
      mount --bind "/dev/$dev" "$BUILD_DIR/chroot/dev/$dev" 2>/dev/null || true
    fi
  done
fi
echo "== lb chroot (this is the long one)"
lb chroot
echo "== lb binary"
lb binary

echo "== done: $(ls -1 "$BUILD_DIR"/*.iso 2>/dev/null || echo 'no iso produced')"
