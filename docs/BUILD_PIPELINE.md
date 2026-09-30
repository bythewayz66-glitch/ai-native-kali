# The AI-native Kali build pipeline

This document describes what the pipeline actually builds, what it deliberately
does not build, and how to verify either claim. It is written so that a reader who
has only the build output can reproduce and check it.

---

## 1. The boundary, stated first

There are two halves, and only one of them runs in a plain container.

| Half | Command | Needs | Produces |
|---|---|---|---|
| **Staged build** | `make build` | `dpkg-deb`, `tar`, `sha256sum` | `.deb` metapackages, the chroot overlay, `BUILD-MANIFEST.json` |
| **Full ISO** | `make iso-full` | the above **plus** `live-build`, `xorriso`, root, network | a bootable `.iso` |

`make build` completes anywhere. `make iso-full` is a thin wrapper around
`live-build` and **fails loudly with a specific message** when the host lacks it.

> **Why the boundary is drawn here rather than blurred.** A build script that
> silently produced a tarball and called it an ISO would be the worst possible
> outcome: the pipeline would look green, `BUILD-MANIFEST.json` would be trusted,
> and the artifact someone tried to boot would not be bootable. The manifest
> therefore carries `"bootable_iso": false` explicitly, and the staged builder's
> own notes name what is still required. Both are asserted by tests.

---

## 2. What the staged build produces

```
var/build/
├── BUILD-MANIFEST.json                # path, bytes and sha256 of every artifact
├── ai-native-kali-rootfs.tar.gz       # the chroot overlay live-build would consume
└── debs/
    ├── kali-ai-native-core_0.1.0_all.deb
    ├── kali-ai-native-shell_0.1.0_all.deb
    └── kali-ai-native-agents_0.1.0_all.deb
```

### 2.1 The metapackages

Built with `dpkg-deb --build --root-owner-group`, then verified by asking `dpkg-deb`
for the package name and version back rather than trusting the exit code. Each
package ships a real payload:

- `usr/share/doc/<pkg>/README.md` — operator documentation
- `usr/share/kali-ai/<pkg>/component.json` — machine-readable declaration of the
  components, ports, systemd units and safety defaults

An empty metapackage would be metadata-only and would install nothing, so the
tests assert the payload is present.

### 2.2 The chroot overlay

The tree `live-build` drops into `config/includes.chroot/`:

| Path | Contents |
|---|---|
| `opt/ai-native-kali/<component>/` | the seven service source trees |
| `etc/systemd/system/` | `kali-ai.target`, the service units |
| `usr/share/wayland-sessions/hermes-shell.desktop` | the session entry |
| `usr/bin/kali-ai-setup` | the first-boot wizard (executable bit preserved) |
| `opt/ai-native-kali/metapackages/` | for the in-chroot install hook |
| `var/lib/kali-ai/{,models}` | first-boot state directories |

`__pycache__`, `*.pyc`, `.pytest_cache`, `node_modules` and `var/` are excluded.
Shipping bytecode caches into an image is a small but real and common mistake, so
a test asserts none are present.

### 2.3 Reproducibility

`SOURCE_DATE_EPOCH` is pinned (default `1700000000`, overridable) and `tar` is
called with `--sort=name --mtime=@$SOURCE_DATE_EPOCH --owner=0 --group=0
--numeric-owner`.

This is not cosmetic. Without it `dpkg-deb` and `tar` embed the wall-clock time,
so two builds of an identical tree produce different bytes — which would make the
manifest's `sha256` a checksum of *when you built* rather than of *what you
built*. Two tests rebuild and assert byte-identical output for both the debs and
the overlay.

---

## 3. How to verify

```bash
make build          # produce var/build/
make verify-build   # re-hash every artifact against the manifest
```

`scripts/verify_build.py` reads the manifest and then **distrusts it**: it checks
each artifact exists, that its size matches, and that its sha256 matches. Exit
codes are distinct so CI can tell the failures apart:

| Exit | Meaning |
|---|---|
| `0` | every artifact present and hashing correctly |
| `1` | a mismatch, or the manifest is missing/unreadable |
| `2` | a required artifact is absent entirely |

`make verify-build` is deliberately separate from `make build`. A build that
produced nothing would pass a test that only ran the builder.

---

## 4. Producing a bootable ISO

On a Debian/Kali host with root:

```bash
sudo apt-get install live-build xorriso
sudo make iso-full
```

`packaging/build-iso.sh` stages the same tree into a live-build working directory,
runs `lb config`, `lb build`, and reports the resulting `.iso`. It exits non-zero
with an instructive message if `lb` is absent or the user is not root — it never
attempts a partial build and calls it done.

---

## 5. Honest status

| Item | State |
|---|---|
| Metapackages | **built and verified** here |
| Chroot overlay | **built and verified** here |
| Reproducible bytes | **verified** (two builds, identical hashes) |
| Manifest + verifier | **implemented and tested** |
| live-build config tree | **present** (`auto/config`, package lists, hooks) |
| Bootable `.iso` | **not built** — requires `live-build` + `xorriso`, absent in this environment |

The bootable image is the only remaining step, and it is blocked on host tooling
rather than on anything in this repository.
