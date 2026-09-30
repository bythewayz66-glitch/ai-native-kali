# Installing and running AI-native Kali locally

This guide is written from the **actual repository state** (`documents/ai-native-kali_v8`).
Every command below was run against that tree; the outputs quoted are real. Where a
step cannot be completed on a plain Linux box, it says so plainly instead of implying
otherwise.

There are three ways to get this running, in order of how little they ask of you:

| # | Path | What you get | Needs | Verified here? |
|---|---|---|---|---|
| **1** | **Run from source** (`make dev`) | the full seven-service stack, live, on `127.0.0.1` | Python 3.11 + pip | ✅ **yes** |
| **2** | **Staged build** (`make build`) | three `.deb` metapackages + a chroot overlay tarball | `dpkg-deb`, `tar`, `sha256sum` | ✅ **yes** |
| **3** | **Bootable ISO** (`make iso-full`) | a bootable image that boots to the Hermes session | a **Kali host**, root, `live-build`, `xorriso`, `debootstrap` | ❌ **no — blocked, see §3** |

> **The one-line answer.** If you just want to *use* it, do **§1** — it works right now
> on any Linux box with Python. If you want the *installable artifacts*, do **§2**.
> The bootable ISO (**§3**) genuinely cannot be built outside a Kali host; this
> repository's own sandbox has no `live-build`, no `xorriso` and no `debootstrap`, and
> `make iso-full` exits 1 with a named error rather than pretending.

---

## 0. Prerequisites

### 0.1 For paths 1 and 2 (any Linux)

```bash
python3 --version          # 3.11 or newer
python3 -m pip --version
node --version             # optional: only for `make test-js`
```

Path 2 additionally needs `dpkg-deb`, `tar` and `sha256sum` — present on any
Debian/Ubuntu/Kali host, and on this sandbox.

### 0.2 Getting the tree

The project is delivered as a directory (there is no `.git` in the delivered tree).
Copy it wherever you want to work:

```bash
cp -r ai-native-kali_v8 ~/ai-native-kali
cd ~/ai-native-kali
```

Everything below is run from the repository root.

---

## 1. Run from source — the dev path ✅ verified

This is the fastest way to a working system. There is **no install step**: every
component imports the others straight from the source tree via a shared `PYTHONPATH`,
which `scripts/dev.sh` sets for you.

### 1.1 Install the Python dependencies (one time)

```bash
python3 -m pip install -r requirements-dev.txt
```

That file pulls in `fastapi`, `uvicorn[standard]`, `pydantic`, `httpx`, `pytest`,
`pytest-asyncio`, `websockets` and `PyYAML`. `websockets` is what lets the bridge claim
cards from the event stream; without it the bridge falls back to polling (correct, but
slower — the smoke test asserts zero fallback polls, so you want it installed).

### 1.2 Bring the stack up

```bash
make dev
```

Real output from this run:

```
booting AI-native Kali stack from /workspace/documents/ai-native-kali_v8
  tool-frontends -> :8083 (pid 14931, log var/log/tool-frontends.log)
  kanban-core -> :8081 (pid 14933, log var/log/kanban-core.log)
  observability -> :8084 (pid 14935, log var/log/observability.log)
  agent-runtime -> :8082 (pid 14937, log var/log/agent-runtime.log)
  hermes-shell -> :8085 (pid 14939, log var/log/hermes-shell.log)
  board-ui -> :8086 (pid 14941, log var/log/board-ui.log)
  memory-store -> :8087 (pid 14943, log var/log/memory-store.log)
waiting for health...
all 7 services healthy
```

`make dev` starts each service in the background, writes a PID file to `var/run/` and a
log to `var/log/`, then polls all seven `/health` endpoints until they answer (up to
30 s). It exits non-zero if any service fails to come up.

### 1.3 What is running, and where

| Service | Port | URL to open | What it is |
|---|---|---|---|
| **board-ui** | 8086 | http://127.0.0.1:8086/ | **Interactive board** — drag cards between lanes, approve gates inline |
| **hermes-shell** | 8085 | http://127.0.0.1:8085/panel | **Desktop shell panel** — live board, taskbar, notification feed, card drawer |
| **observability** | 8084 | http://127.0.0.1:8084/dashboard | **Dashboard** — timeline, traces, tokens, audit chain, model inspector |
| **memory-store** | 8087 | http://127.0.0.1:8087/docs | **L6 memory** — episodes, facts, search, vector recall, knowledge graph |
| **kanban-core** | 8081 | http://127.0.0.1:8081/docs | Orchestration backbone REST API |
| **agent-runtime** | 8082 | http://127.0.0.1:8082/health | Kanban ⇄ CrewAI bridge |
| **tool-frontends** | 8083 | http://127.0.0.1:8083/tools | Guardrailed Kali tool layer (73 wrappers) |

All services bind to `127.0.0.1` only — nothing is exposed off the machine.

### 1.4 Prove it works

```bash
make status        # one-line health per service
make smoke         # the end-to-end proof (155 checks)
make test          # the full Python suite
make test-js       # the board's drag-and-drop logic under Node
make mcp           # drive the tool layer over the real MCP stdio transport
```

Real output from this run:

```
$ make status
AI-native Kali service status
  kanban-core      :8081  UP    {"status":"ok","service":"kanban-core",...,"boards":4,...}
  agent-runtime    :8082  UP    {"status":"ok","service":"agent-runtime",...,"backend":"local",...}
  tool-frontends   :8083  UP    {"status":"ok","service":"tool-frontends",...,"tools":{"count":73,...}}
  observability    :8084  UP    {"status":"ok","service":"observability",...,"loop_running":true,...}
  hermes-shell     :8085  UP    {"status":"ok","service":"hermes-shell",...,"surfaces":[...]}
  board-ui         :8086  UP    {"status":"ok","component":"board-ui",...}
  memory-store     :8087  UP    {"status":"ok","service":"memory-store",...,"episodes":84,...}

$ make smoke
  ...
  155/155 checks passed
  full loop verified: card -> crew -> tool -> trace -> audit -> Review

$ make test
  1910 passed, 12 skipped in 62.43s
```

### 1.5 Stop it

```bash
make stop          # stops every service started by `make dev`
make clean         # stop + delete var/db, var/log, var/run, var/build
```

### 1.6 Running a single component

Useful when you are working on one service. Export the shared path once:

```bash
export PYTHONPATH="$PWD/kanban-core:$PWD/tool-frontends:$PWD/agent-runtime:$PWD/observability:$PWD/hermes-shell:$PWD/board-ui:$PWD/memory-store"

python3 -m uvicorn kanban_core.api:app        --port 8081
python3 -m uvicorn tool_frontends.server:app  --port 8083
python3 -m uvicorn agent_runtime.server:app   --port 8082
python3 -m uvicorn observability.server:app   --port 8084
python3 -m uvicorn hermes_shell.server:app    --port 8085
python3 -m uvicorn board_ui.server:app        --port 8086
python3 -m uvicorn memory_store.server:app    --port 8087
```

Tests for one component:

```bash
python3 -m pytest tool-frontends -p no:cacheprovider
```

### 1.7 Docker alternative

If you would rather not install Python packages on the host:

```bash
docker compose up --build
```

`docker-compose.yml` mirrors `make dev` (same services, same ports) and pins live tool
execution **off** (`TOOLS_LIVE=0`, `TOOLS_UNLOCK` empty). Note the compose file covers
the six original services; `memory-store` (8087) is not in it — use `make dev` if you
want the full seven.

### 1.8 Safety defaults you should know about

These are on by default and are the reason the stack is safe to run on a laptop:

- **`TOOLS_LIVE=0`** — every tool call is a **dry run**. Nothing executes.
- **`TOOLS_UNLOCK`** is empty — no tool is unlocked for live execution.
- **`TOOLS_MAX_TIER=3`** — the ceiling, not a grant.
- **`MODEL_ENABLED=0`** — no model is contacted; crews use the deterministic local runner.
- **`MEMORY_ENABLED=1`** in dev (so the recall path is exercised), optional in production.

To run a tool for real you must set **both** `TOOLS_LIVE=1` **and** name the tool in
`TOOLS_UNLOCK`. A refused live request returns an explicit denial — it never silently
downgrades to a dry run.

---

## 2. Build and install the staged artifacts ✅ verified

This is the half of the ISO pipeline that runs anywhere. It produces the two things the
bootable build consumes, and it is reproducible.

### 2.1 Build

```bash
make build          # == bash packaging/build.sh
```

Real output from this run:

```
[build] building kali-ai-native-core
[build] building kali-ai-native-shell
[build] building kali-ai-native-agents
[build]   ok: kali-ai-native-core 0.1.0 (2116 bytes)
[build]   ok: kali-ai-native-shell 0.1.0 (1948 bytes)
[build]   ok: kali-ai-native-agents 0.1.0 (2096 bytes)
[build] staging chroot overlay
[build] packing rootfs overlay
[build]   rootfs: 372459 bytes, 187 entries
[build] writing manifest
[build] manifest valid JSON
[build] done -> .../var/build
```

Output tree:

```
var/build/
├── BUILD-MANIFEST.json                # path, bytes and sha256 of every artifact
├── ai-native-kali-rootfs.tar.gz       # the chroot overlay
└── debs/
    ├── kali-ai-native-core_0.1.0_all.deb
    ├── kali-ai-native-shell_0.1.0_all.deb
    └── kali-ai-native-agents_0.1.0_all.deb
```

### 2.2 Verify it

```bash
make verify-build   # == python3 scripts/verify_build.py var/build
```

Real output:

```
verifying 4 artifact(s) in var/build
  project=ai-native-kali version=0.1.0 kind=staged-build
  ok       debs/kali-ai-native-agents_0.1.0_all.deb (2096 bytes, 700f2866cacf8873...)
  ok       debs/kali-ai-native-core_0.1.0_all.deb (2116 bytes, 05dfc8b68c04b78e...)
  ok       debs/kali-ai-native-shell_0.1.0_all.deb (1948 bytes, acaf623151afed3a...)
  ok       ai-native-kali-rootfs.tar.gz (372459 bytes, 19ac492cddb1e80a...)
total 378619 bytes
OK: every artifact present and hashing correctly
```

The verifier **distrusts the manifest**: it re-hashes every file and compares. Exit codes
are distinct — `0` ok, `1` mismatch, `2` a required artifact is missing.

### 2.3 What is actually inside each artifact — read this before installing

This matters, because the two artifacts do different jobs:

**The `.deb` files are metapackages.** They carry documentation and a machine-readable
`component.json`, plus `Depends:` declarations — they do **not** contain the service
code. Verified contents of `kali-ai-native-core_0.1.0_all.deb`:

```
./usr/share/doc/kali-ai-native-core/README.md
./usr/share/kali-ai/kali-ai-native-core/component.json
```

**The service code lives in the rootfs overlay tarball**, under `opt/ai-native-kali/`:

```
$ tar -tzf var/build/ai-native-kali-rootfs.tar.gz | grep '^./opt/ai-native-kali/'
./opt/ai-native-kali/agent-runtime/
./opt/ai-native-kali/hermes-shell/
./opt/ai-native-kali/kanban-core/
./opt/ai-native-kali/memory-store/
./opt/ai-native-kali/metapackages/
./opt/ai-native-kali/observability/
./opt/ai-native-kali/tool-frontends/
```

The overlay also carries the systemd units, the session entry and the first-boot wizard:

```
./etc/systemd/system/kali-ai.target
./usr/bin/hermes-shell-session          (mode 0755)
./usr/bin/kali-ai-setup                 (mode 0755)
./usr/share/wayland-sessions/hermes-shell.desktop
```

> **Note:** the overlay ships **six** components — `board-ui` is not included (it is a
> dev-only surface). The six are `kanban-core`, `tool-frontends`, `agent-runtime`,
> `observability`, `hermes-shell`, `memory-store`.

### 2.4 Install the metapackages (optional, metadata only)

```bash
sudo dpkg -i var/build/debs/*.deb
```

This registers the three metapackages and installs their docs and `component.json`. It
does **not** give you a running system on its own — the services come from the overlay
(§2.5). If `dpkg` complains about dependencies, `sudo apt-get -f install` resolves them.

### 2.5 Install and run the overlay

**Option A — install to `/` (what the ISO does):**

```bash
sudo tar -xzf var/build/ai-native-kali-rootfs.tar.gz -C /
```

That places the services at `/opt/ai-native-kali/`, the units in
`/etc/systemd/system/`, and the session entry at `/usr/bin/hermes-shell-session`.

**Option B — extract to a prefix and run from there (no root, no system changes):**

```bash
mkdir -p /tmp/kali-ai-root
tar -xzf var/build/ai-native-kali-rootfs.tar.gz -C /tmp/kali-ai-root

export PYTHONPATH="/tmp/kali-ai-root/opt/ai-native-kali/kanban-core:\
/tmp/kali-ai-root/opt/ai-native-kali/tool-frontends:\
/tmp/kali-ai-root/opt/ai-native-kali/agent-runtime:\
/tmp/kali-ai-root/opt/ai-native-kali/observability:\
/tmp/kali-ai-root/opt/ai-native-kali/hermes-shell:\
/tmp/kali-ai-root/opt/ai-native-kali/memory-store"

python3 -m uvicorn kanban_core.api:app --port 8081
```

**Run the services under systemd** (Option A only):

```bash
sudo systemctl start kali-ai.target
systemctl status kali-ai.target
journalctl -u hermes-shell.service -n 50
```

`kali-ai.target` is the umbrella unit; it pulls in `kali-ai-kanban.service`,
`kali-ai-observability.service` and `hermes-shell.service`.

### 2.6 Reproducibility

`SOURCE_DATE_EPOCH` is pinned (default `1700000000`), and `tar` is called with
`--sort=name --mtime=@$SOURCE_DATE_EPOCH --owner=0 --group=0 --numeric-owner`. Two
builds of the same tree produce **byte-identical** artifacts, which is what makes the
manifest's `sha256` a checksum of *what* you built rather than *when*. Override with:

```bash
SOURCE_DATE_EPOCH=1700000000 make build
```

---

## 3. The bootable ISO — ❌ NOT buildable here, and why

**No bootable image was produced by this repository's run, and none is claimed.** This
section tells you exactly what a Kali host needs and exactly what blocked it here.

### 3.1 What blocked it in this sandbox

`make iso-full` was run. Real output:

```
$ make iso-full
live-build ('lb') is not installed: apt-get install live-build
make: *** [Makefile:88: iso-full] Error 1
```

The host check:

```
$ for t in lb xorriso debootstrap; do command -v $t || echo "$t MISSING"; done
lb MISSING
xorriso MISSING
debootstrap MISSING
$ id -u
0
```

So: **root is available, but the three build tools are not, and they cannot be installed
in this sandbox.** `packaging/profiles.py::preflight()` reports the same thing in code:

```json
{
  "ok": false,
  "tools": {"lb": false, "xorriso": false, "debootstrap": false},
  "missing_tools": [
    "lb - the live-build driver (`apt install live-build`)",
    "xorriso - the ISO image writer (`apt install xorriso`)",
    "debootstrap - the chroot builder (`apt install debootstrap`)"
  ],
  "default_profile": "minimal",
  "needs_root": true
}
```

### 3.2 What a Kali host needs

**Operating system.** Kali (recommended) or Debian with the Kali archive added — the
package list names Kali packages (`kali-linux-core`, `kali-tools-*`), so a plain Debian
host must add the archive first or `lb build` fails resolving them.

```bash
# On Debian only (skip on Kali):
echo "deb https://http.kali.org/kali kali-rolling main contrib non-free non-free-firmware" \
  | sudo tee /etc/apt/sources.list.d/kali.list
sudo apt-get update
```

**Packages.**

```bash
sudo apt-get install -y \
  live-build xorriso debootstrap \
  dpkg-dev \
  python3 python3-venv python3-gi gir1.2-gtk-4.0 \
  libgtk4-layer-shell0 python3-cairo \
  rsync git curl ca-certificates
```

| Package | Why | If missing |
|---|---|---|
| `live-build` | provides `lb`, which `build-iso.sh` drives | pipeline exits 1 with a named error |
| `xorriso` | writes the bootable image | `lb build` fails at the ISO step |
| `debootstrap` | builds the chroot | `lb build` fails at the bootstrap step |
| `python3-gi`, `gir1.2-gtk-4.0` | the Hermes GTK session binary | session entry exists but cannot start |
| `libgtk4-layer-shell0` | anchored panel role | session degrades to a plain toplevel (logged, not fatal) |
| `python3-cairo` | `--snapshot` rendering | snapshot flag reports the gap instead of writing a PNG |

> **Do not add `gir1.2-webkit2-4.1`.** The shell draws with native GTK 4 widgets
> precisely because the WebKitGTK on Debian bases is 4.1, which is GTK-3-based and
> cannot be embedded in a GTK 4 window. Adding it makes the client unimportable.

**Privileges.** `lb build` **requires root** — it debootstraps a chroot, mounts
`/proc`, `/sys`, `/dev/pts` inside it, and writes loopback filesystems. The staged half
(§2) does **not** need root.

**Disk and time.**

| Resource | Requirement |
|---|---|
| Free disk | **~8 GB** in `$BUILD_DIR` (default `/tmp/ai-native-kali-build`) |
| Time | **20–45 min** warm apt cache; **60–90 min** cold |
| Network | required — `lb` fetches every package in the list |
| RAM | 2 GB minimum, 4 GB comfortable |

### 3.3 The exact commands

```bash
# 1. Staged half — no root, ~20 s. (Same as §2.)
bash packaging/build.sh
make verify-build

# 2. Bootable half — root + live-build + xorriso. 20–90 min.
sudo AI_REQUIRE_XORRISO=1 bash packaging/build-iso.sh

# 3. Inspect the result.
ls -la /tmp/ai-native-kali-build/*.iso
sha256sum /tmp/ai-native-kali-build/*.iso
```

Or via make:

```bash
make build
make verify-build
sudo make iso-full
```

`BUILD_DIR` overrides the output directory. `AI_PROFILE` selects the hardware profile
(`minimal` default, or `workstation` / `gpu`); `BUNDLE_STAGE` points at staged model
weights (default `/var/lib/kali-ai/models`).

> **Preflight note (honest).** `docs/BUILD_HOST.md` §3.1 shows a preflight block that
> `packaging/build-iso.sh` does **not** currently contain — the script checks `lb` and
> root, then proceeds. The full check exists as `packaging/profiles.py::preflight()`
> and can be run directly:
> ```bash
> python3 -c "import sys; sys.path.insert(0,'packaging'); import json,profiles; print(json.dumps(profiles.preflight(), indent=2))"
> ```
> Run that **before** a 90-minute build. (This discrepancy is recorded as a defect in
> `BUILD_STATUS.md`.)

### 3.4 The offline model bundle (optional)

The image boots and works without a model — crews use the deterministic runner. To ship
a model so the semantic embedder is the out-of-the-box default, stage the weights on a
networked host first:

```bash
# See exactly what would be pulled (no side effects):
python3 packaging/fetch_bundle.py plan --profile workstation

# Stage the weights (needs a reachable Ollama):
OLLAMA_MODELS=/var/lib/kali-ai/models ollama pull qwen2.5:3b-instruct-q4_K_M
OLLAMA_MODELS=/var/lib/kali-ai/models ollama pull nomic-embed-text:latest

# Verify what you staged:
python3 packaging/fetch_bundle.py verify --profile workstation --stage-dir /var/lib/kali-ai/models
```

`build-iso.sh` copies `/var/lib/kali-ai/models` into the image when it is present, and
the chroot hook writes `/etc/kali-ai/model-bundle.env` from the manifest so the runtime
and the bundle cannot disagree. **A bundle that is absent is not an error**; a bundle
that is present and corrupt is, and `verify` is what catches that.

> **Honest note:** `docs/BUILD_HOST.md` §4 refers to a `packaging/model-bundle.sh`. That
> script does not exist — the real tool is `packaging/fetch_bundle.py` (the commands
> above). Recorded as a defect in `BUILD_STATUS.md`.

### 3.5 Booting the result to the Hermes session

Once you have an ISO on a Kali host:

```bash
qemu-system-x86_64 -m 4096 -cdrom live-image-amd64.hybrid.iso \
  -boot d -enable-kvm -vga virtio
```

1. At the greeter, choose the **Hermes AI Desktop** session.
2. Expected: the GTK panel starts, the taskbar renders, the overlay appears; the units
   in `kali-ai.target` bring up kanban-core and observability behind it.
3. If the session dies back to the greeter, check the entry point exists:
   ```bash
   ls -l /usr/bin/hermes-shell-session
   systemctl status kali-ai.target
   journalctl -u hermes-shell.service -n 50
   ```

**No boot log is quoted here because none was captured** — no image was built.

---

## 4. What is verified where

| Step | Verified in this sandbox | Needs a Kali host |
|---|---|---|
| `make dev` — seven services healthy | ✅ | — |
| `make smoke` — 155/155 checks | ✅ | — |
| `make test` — 1910 passed, 12 skipped | ✅ | — |
| `make test-js` — board logic under Node | ✅ | — |
| `make mcp` — MCP stdio session | ✅ | — |
| `make build` — debs + overlay + manifest | ✅ | — |
| `make verify-build` — manifest re-hashed | ✅ | — |
| Overlay contains the session entry at 0755 | ✅ | — |
| `fetch_bundle.py plan` / `verify` | ✅ | — |
| `make iso-full` — bootable image | ❌ blocked (`lb` absent) | ✅ |
| Booting the image to the Hermes session | ❌ not attempted | ✅ |
| GTK panel on a real Wayland session | ⚠️ degrades to `toplevel` (no layer-shell here) | ✅ for the anchored path |

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `make dev` says "missing dependencies" | pip deps not installed | `python3 -m pip install -r requirements-dev.txt` |
| `make dev` exits 1, "services did not all become healthy" | a service failed to start | read `var/log/<service>.log` |
| `make smoke` reports fallback polls > 0 | `websockets` not installed | `python3 -m pip install websockets` |
| `make iso-full` → "live-build ('lb') is not installed" | not a Kali build host | see §3.2 — this is expected off a Kali host |
| `make gtk-client` exits 2 | no Wayland session / no PyGObject | expected on X11 or a bare container; the client degrades to the browser panel |
| `dpkg -i` dependency errors | metapackage `Depends:` unmet | `sudo apt-get -f install` |
| Port already in use | a previous run is still up | `make stop`, then `make dev` |

---

## 6. Where to go next

| Document | Read it for |
|---|---|
| `README.md` | the architecture and the one idea behind it |
| `BUILD_STATUS.md` | exactly what is implemented, stubbed, and next |
| `docs/BUILD_HOST.md` | the build-host checklist in full |
| `docs/BUILD_PIPELINE.md` | what the pipeline builds and how to verify it |
| `docs/VERIFICATION.md` | the captured acceptance runs |
| `docs/T2_TOOL_CALL_DATAFLOW.md` | one gated tool call traced end to end |
