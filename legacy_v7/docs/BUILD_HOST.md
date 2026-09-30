# Building the bootable ISO on a Kali host

Phase 7, item 4. This is the document the Phase 6 report said was missing: exactly
what a build host needs, what it will produce, and how to verify what it produced.

**Read this first:** the sandbox this repository is developed in **cannot** build a
bootable image — but not for the reason an earlier revision of this document gave. The
tooling *can* be installed there (`live-build`, `xorriso`, `debootstrap` all install, and
root is available), and the build was run for real: it downloaded the entire Kali base
(~186 MB) and then stopped at debootstrap's device-node step because the sandbox's seccomp
policy denies `mknod(2)` even with `CAP_MKNOD` present. Disk (3.6 GB free) and a missing
`mksquashfs` are the other two blockers. Everything up to and including the chroot overlay
*is* built and verified here on every run; the final `lb build` is not. That boundary is
enforced in code, not in prose — see "Honest status" at the end.

---

## 1. What the two halves are

The pipeline is deliberately split so that the half which can run anywhere does.

| Half | Command | Needs | Produces |
|---|---|---|---|
| **Staged** | `bash packaging/build.sh` | `dpkg-deb`, `tar`, `sha256sum` — **no root** | 3 `.deb` metapackages, a chroot overlay tarball, `BUILD-MANIFEST.json` |
| **Bootable** | `bash packaging/build-iso.sh` (or `make iso-full`) | `live-build` (`lb`), `xorriso`, **root**, ~8 GB free | `live-image-amd64.hybrid.iso` |

The staged half is what the bootable half *consumes*. If you run only the first, you
have a verifiable tree with no image. If you run the second, it runs the first and
then wraps it.

---

## 2. Build host requirements

### 2.1 Operating system

A **Debian or Kali** host. Kali is recommended because the package list in
`packaging/live-build/config/package-lists/kali-ai-native.list.chroot` names Kali
packages; a plain Debian host must first add the Kali archive, or the `lb build` step
will fail resolving `kali-linux-headless` and similar.

```bash
# On Debian, add Kali (skip on a Kali host):
echo "deb https://http.kali.org/kali kali-rolling main contrib non-free non-free-firmware" \
  | sudo tee /etc/apt/sources.list.d/kali.list
sudo apt-get update
```

### 2.2 Packages

```bash
sudo apt-get install -y \
  live-build xorriso squashfs-tools \
  dpkg-dev debootstrap \
  python3 python3-venv python3-gi gir1.2-gtk-4.0 \
  libgtk4-layer-shell0 python3-cairo \
  rsync git curl ca-certificates
```

| Package | Why it is needed | Consequence of it missing |
|---|---|---|
| `live-build` | provides `lb`, which the `build-iso.sh` script drives | pipeline exits 1 with a named error |
| `xorriso` | actually writes the bootable image | `lb build` fails at the ISO step; `AI_REQUIRE_XORRISO=1` makes this a hard preflight failure |
| `debootstrap` | builds the chroot `lb` installs into | `lb build` fails at the bootstrap step |
| `python3-gi`, `gir1.2-gtk-4.0` | the Hermes GTK session binary | the session entry point exists but cannot start |
| `libgtk4-layer-shell0` | anchored panel role for the overlay | session degrades to a plain toplevel (logged, not fatal) |
| `python3-cairo` | `--snapshot` rendering path | snapshot flag reports the gap rather than writing a PNG |

> **`WebKitGTK 6.0` note.** The shell does **not** need WebKit — the Phase 6 client
> draws with native GTK 4 widgets precisely because the WebKitGTK present on Debian
> bases is 4.1, which is GTK-3-based and cannot be embedded in a GTK 4 window. Do not
> add `gir1.2-webkit2-4.1` hoping to "upgrade" the shell; it will make the client
> unimportable.

### 2.3 Privileges

`lb build` **requires root** — it debootstraps a chroot, mounts `/proc`, `/sys`,
`/dev/pts` inside it, and writes loopback-mounted filesystems.

```bash
sudo -i            # or run the make target under sudo
```

The staged half (`packaging/build.sh`) does **not** need root and is deliberately
runnable unprivileged so it can run in CI.

### 2.4 Disk and time

| Resource | Requirement | Note |
|---|---|---|
| Free disk | **~8 GB** in `$BUILD_DIR` | ~2.5 GB chroot + ~700 MB squashfs + working copy. `df -h` first. |
| Time | **20–45 min** on a warm `apt` cache; **60–90 min** cold | the debootstrap + package install dominates |
| Network | required | `lb` fetches every package in the list |
| RAM | 2 GB minimum, 4 GB comfortable | squashfs compression |

The AI/model payload is **not** downloaded here — the offline model bundle is a
separate, explicitly-run step (§4). An ISO built without it boots but has no model,
and the crews run the deterministic fallback (which is a supported mode, not a
failure).

---

## 3. The exact commands

```bash
git clone <this-repo> ai-native-kali && cd ai-native-kali

# 1. Staged half — no root, verifiable anywhere. ~20 s.
bash packaging/build.sh

# 2. Verify what step 1 produced against its own manifest. ~1 s.
make verify-build

# 3. Bootable half — needs root + live-build + xorriso. 20–90 min.
sudo AI_REQUIRE_XORRISO=1 bash packaging/build-iso.sh

# 4. Verify the image. ~10 s.
ls -la /tmp/ai-native-kali-build/*.iso
sha256sum /tmp/ai-native-kali-build/*.iso
```

Or, in one step, the make targets:

```bash
make build          # staged half   (== packaging/build.sh)
make verify-build   # manifest check
sudo make iso-full  # bootable half (== packaging/build-iso.sh)
```

`BUILD_DIR` overrides the output directory (default `/tmp/ai-native-kali-build`).

### 3.1 Preflight — check the host *before* a 90-minute build

`packaging/build-iso.sh` runs a preflight block first and reports **every** missing
requirement at once rather than dying on the first:

```
== preflight
   ok      root privileges
   ok      live-build (lb)
   ok      xorriso
   ok      debootstrap
   ok      xorriso present (required by AI_REQUIRE_XORRISO=1)
   ok      8.1 GB free in /tmp/ai-native-kali-build
== staging /tmp/ai-native-kali-build
```

Each failing line names the `apt-get install` that fixes it. This exists because the
alternative — discovering `xorriso` is missing after a 45-minute bootstrap — is the
way an ISO build wastes an afternoon.

---

## 4. The offline model bundle

`packaging/model-bundle.sh` populates `var/model-bundle/` with the weights a
no-network image needs. It is **opt-in**, because it is large and needs a model host.

```bash
# On a host with a reachable Ollama and the model already pulled:
bash packaging/model-bundle.sh --model llama3.1 --embed-model nomic-embed-text

# Verify the bundle before it is baked into an image:
bash packaging/model-bundle.sh --verify
```

Output: `var/model-bundle/MANIFEST.json` (model names, sizes, sha256) plus the
weights. `build.sh` copies it into the overlay at
`/var/lib/kali-ai/models` when present; when absent, the image still builds and
`/var/lib/kali-ai/models` is created empty. **A bundle that is absent is not an
error** — the crews fall back to the deterministic runner — but a bundle that is
*present and corrupt* is, and `--verify` is what catches that.

---

## 5. Expected artifacts and checksums

After `bash packaging/build.sh` (deterministic — `SOURCE_DATE_EPOCH` is pinned, so
two builds of the same tree produce the *same* bytes):

| Artifact | Path (relative to `var/build/`) | Approx. size |
|---|---|---|
| Metapackage | `debs/kali-ai-native-core_0.1.0_all.deb` | ~2.1 KB |
| Metapackage | `debs/kali-ai-native-shell_0.1.0_all.deb` | ~1.9 KB |
| Metapackage | `debs/kali-ai-native-agents_0.1.0_all.deb` | ~2.1 KB |
| Chroot overlay | `ai-native-kali-rootfs.tar.gz` | ~250 KB |
| Manifest | `BUILD-MANIFEST.json` | — |

**Do not trust these figures as checksums.** The manifest in *your* build is
authoritative for *your* build; `make verify-build` recomputes every sha256 from the
files on disk and compares them against the manifest, failing on any mismatch or any
missing file. Copy the real values out of your own `var/build/BUILD-MANIFEST.json`.

`BUILD-MANIFEST.json` carries `"bootable_iso": false` — and it must keep saying that,
because the staged build genuinely did not make one. The ISO's own path is printed by
`build-iso.sh` on completion. If that key ever reads `true` from `build.sh` alone,
**the manifest is lying** and `tests/test_phase7_packaging.py` fails.

---

## 6. Verifying the image boots to the Hermes session

The point of the exercise is a session, not just a file. On the built image:

1. Boot it (QEMU is enough to prove it, a spare machine is better):
   ```bash
   qemu-system-x86_64 -m 4096 -cdrom live-image-amd64.hybrid.iso \
     -boot d -enable-kvm -vga virtio
   ```
2. At the greeter, choose the **Hermes Session** entry.
3. Expected: the GTK panel starts, the taskbar renders, and the overlay appears.
   The units in `etc/systemd/system/kali-ai.target` bring up kanban-core and
   observability behind it.
4. If the session dies back to the greeter, the usual cause is a missing
   `/usr/bin/hermes-shell-session` (the Phase 6 defect) — check
   `ls -l /usr/bin/hermes-shell-session` inside the live system. Phase 6 fixed the
   *build* to install it; this is the check that it survived.

```bash
# On the running live system:
systemctl status kali-ai.target
journalctl -u hermes-shell.service -n 50
```

---

## 7. Honest status — read before quoting anything from this document

**What is verified in CI/sandbox, every run:**

- the staged half builds, reproducibly (identical sha256 across runs);
- `make verify-build` recomputes and matches the manifest;
- all 3 metapackages pass `dpkg-deb -f` name/version checks;
- the rootfs overlay contains `usr/bin/hermes-shell-session` at mode 0755 and the
  `.desktop` `Exec=` target resolves (Phase 6 defect #19);
- the manifest declares `bootable_iso: false`;
- the preflight reports *every* missing host requirement at once.

**What is NOT verified here, and why:**

- **No bootable image has been produced or booted by this repository's test run.** The
  tooling installs and the build runs, but it stops at debootstrap's `mknod` step because
  the sandbox's seccomp policy denies `mknod(2)` despite `CAP_MKNOD` being present; disk
  (3.6 GB free) and a missing `mksquashfs` block the later stages too. `make iso-full` exits
  1 with a named error. Any claim that an ISO exists would be false.
- Hardware profiles and the offline bundle are implemented and unit-tested, but no
  image containing them has been booted.

A Kali host with §2 satisfied closes both gaps. Until then the honest claim is: **the
pipeline is complete, exercised up to the ISO boundary, and fails loudly at it.**
