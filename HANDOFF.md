# HANDOFF — AI-native Kali

> ### Phase 15 (latest, 2026-10-06) — the Dream list, items 1–10
>
> **Tree:** `documents/ai-native-kali_v19` — a copy of the Phase 14 head
> (`a81a51c`), made per the task-continuation rule. **Base commit:**
> `a81a51c3038d981556273efcf34f2c5e9a575280`; this round's tip is the pushed
> `main` (see `git log -1`).
>
> **What changed:**
> - **Item 1** MCP stdio transport — already built; handshake verified end to end
>   (74 tools, nmap dry-run). **Fixed** the example client sending `profile` to
>   `nmap_scan` (which declares `ports`), which made the demo look like a guardrail
>   refusal when it was a client bug.
> - **Item 2** bridge recall-before-run — already built and covered by 21 tests;
>   verified, not rewritten.
> - **Item 3** `hermes_shell/agent_desktop.py` — **new**. Agent-facing window and
>   process surface (+4 routes), sharing the panel's `WindowManager`. Protected
>   windows refuse; the process allow-list is checked against the real `comm`, not
>   the argument; pid 1 and the agent's own pid refuse; dry-run unless `live=True`.
> - **Item 4** `agent_runtime/desktop_orchestration.py` + design doc — **new**.
>   A card's `metadata.surface_window` surfaces the window **through the shell's own
>   `/api/desktop`** and the decision joins the tool audit chain. Live HTTP PoC
>   captured (window focus moved, ghost card did nothing, protected close refused,
>   `verify_chain: True`).
> - **Item 5** T1 scope gap — **a real escape existed and is fixed.** Seven T1
>   system tools named a host but declared `requires_scope=False`, so a live run
>   with scope `example.com` and `target=evil.net` returned `allowed=True` with no
>   reasons. New `scripts/scope_boundary_audit.py` proves the fix: **53/53 hostile
>   live runs refused, 0 offenders**.
> - **Item 6** boundary audit — **one real defect fixed**: `orchestrator` bound
>   `log_digest`, a tool that was not registered (now it is). `integrity_baseline`
>   had declared a *file path* as its target parameter; now a host. **0 offenders.**
> - **Items 7–10** docs: `docs/unity_license_setup.md`,
>   `docs/hdrp_vs_ue5_comparison.md` (URP-tiered recommended over HDRP so Android
>   stays viable), `docs/local_opencode_workflow.md`, `docs/MILESTONES.md`
>   (**M1 progress review by 2026-10-20**, **C1 teammate contact by 2026-10-13**).
>
> **Gate:** `pytest` **1994 passed / 17 skipped / 0 failed**; `make dev` **7/7
> healthy**; `make smoke` **155/155**; audit **PASS**. Stack stopped after.
>
> **Still open (unchanged from Phase 14):** the ISO was built and boots, but the
> Hermes *session* was not reached in-sandbox (2 GB memory cgroup OOM-kills QEMU);
> boot it on a host with ≥4 GB RAM free. The live crew path needs a larger memory
> cgroup for the 3b model. File-manager drop surface and start-menu polish remain.

**Written:** 2026-10-02 · **Tree:** `ai-native-kali_v11` (this repo root)
**Repo:** `https://github.com/bythewayz66-glitch/ai-native-kali` — a **new, dedicated, PUBLIC**
repo. This project must **never** be pushed to `petrichor` (a separate private Unity/C#
game). The `origin` remote here points at `ai-native-kali`.
**Local commit:** branch `main`, **379 tracked files**, tip
`42076ee66e1f87983dabd9a0116ec46af13adc04`.
**Push status:** ✅ **PUSHED AND VERIFIED.** Remote `main` = local `main` =
`42076ee66e1f87983dabd9a0116ec46af13adc04`; a fresh clone of the pushed tree ran
`make dev && make smoke` → **155/155 checks passed**. See §5.

This document is self-contained: a fresh local agent can continue from here without the
chat that produced it. Read it top to bottom before running anything.

---

## 1. What this project is

An AI-native security OS that fuses **Kali Linux**, **Hermes Desktop** and **CrewAI**.
The one idea: *every unit of work is a card on a board.* Agent tasks, security workflows
and user requests are all cards moving through
`Backlog → Assigned → Running → Review → Done/Blocked`; every tool call a crew makes is
written to a hash-chained audit log. The board is the task queue, the observability
surface and the human control plane at once.

Seven services, all real code, no mocks returning canned data:

| Dir | Service | Port |
|---|---|---|
| `kanban-core/` | Orchestration backbone (cards, state machine, event bus, hash-chained log) | 8081 |
| `agent-runtime/` | Kanban ⇄ CrewAI bridge (event-stream claim, crews, gates, CrewAI adapter) | 8082 |
| `tool-frontends/` | Kali tool layer (73 wrappers, T0–T3 guardrails, scope checks, MCP stdio) | 8083 |
| `observability/` | Collector + dashboard (timeline, traces, tokens, audit chain) | 8084 |
| `hermes-shell/` | Desktop shell panel (GTK4 + layer-shell client) | 8085 |
| `board-ui/` | Interactive drag-and-drop board | 8086 |
| `memory-store/` | L6 memory (episodic + semantic + vector recall + knowledge graph) | 8087 |

---

## 2. Current state — verified in the build run

Run on the build host on 2026-09-30 against this tree:

| Signal | Result |
|---|---|
| Full pytest suite (7 components + `tests/`) | **1952 collected — 1951 passed, 1 failed, 16 skipped** |
| Board logic suite (Node) | **25 passed, 0 failed** |
| `make dev` | all **seven** services healthy (8081–8087) |
| `make smoke` | **155/155 checks passed** |
| Tool wrappers | **73** across 12 categories |
| Documented defects fixed (cumulative) | **23** (Phase 8) |

> **The one failure is expected and is itself a finding.** It is
> `tests/test_phase7_profiles.py::TestPreflight::test_on_this_host_the_preflight_reports_the_real_blocker`,
> which asserts `preflight()["ok"] is False` — i.e. it hardcodes the assumption that
> *this sandbox cannot build the ISO*. In this run the sandbox **does** have `lb`,
> `xorriso` and `debootstrap` installed, so `preflight()` correctly returns `ok: True`
> and the test fails. The test is environment-dependent, not a code regression. On a host
> without the ISO tooling it passes. See §4.

---

## 3. What is done and verified

- **The full loop** — card → crew → tool → trace → audit → `Review`, with the human owning
  `Done`. Verified by `make smoke` (155 checks).
- **Event-driven claim** — a card is claimed over `/ws/events` with **zero fallback polls**
  (asserted by the smoke test).
- **Guardrails** — T0–T3 tiers, dry-run by default, live execution opt-in per tool, scope
  enforced twice (bridge + tool layer), no shell ever, two independent hash chains.
- **MCP stdio transport** — a full session from a subprocess client.
- **Staged build** — `make build` produces 3 `.deb` metapackages, a chroot overlay tarball
  and a manifest whose hashes are checked (`make verify-build`). Runs here.
- **CrewAI adapter** — `agent-runtime/agent_runtime/crewai_adapter.py` **exists** and is
  covered by **20 tests** (`agent-runtime/tests/test_crewai_adapter.py`). It imports
  `crewai` lazily and falls back to the deterministic local runner when the package is
  absent, so there is **no hard dependency**. `crewai_available()` / `crewai_version()` are
  reported on `/health`. **Not yet wired end-to-end** through a live crew run — see §4.3.
- **Sub-card scope model + remediation crew** — design doc
  (`docs/SUB_CARD_SCOPE_MODEL.md`) and enforcement, with the invariant that an *absent*
  scope is the **widest** scope (a parent-scoped / child-unscoped pair is a refusal).
- **Local install guide** — `docs/INSTALL_LOCAL.md`, every command run against this tree.
- **Repo hygiene** — `.gitignore` hardened to exclude staged build output (`var/build/`,
  `dist/`, `*.deb`, `*.tar.gz`), model-bundle weights (`*.gguf`, `*.safetensors`, `*.onnx`,
  `*.pt`, `*.bin`), disk images (`*.img`, `*.qcow2`, `*.vmdk`), coverage/cache dirs and
  editor noise. Verified with `git check-ignore` that **no tracked source file is affected**.
- **Push helper** — `push_to_github.sh` at the tree root: creates the new repo, sets the
  remote and pushes, using a PAT. It **refuses to run if `origin` points at petrichor**.

---

## 4. What is blocked, and why

### 4.1 The bootable ISO — needs a real Kali host

`make iso-full` (`packaging/build-iso.sh`) **cannot complete in the build sandbox.** The
build got further than any previous one and pinned the blocker precisely:

1. **Fixed — the mirror was never named.** `packaging/live-build/auto/config` did not pass
   `--mirror-bootstrap` / `--mirror-chroot` / `--mirror-binary`, so live-build fell back to
   its Debian default and asked it for `dists/kali-rolling/Release` — a path that does not
   exist on the Debian mirror. debootstrap died with
   `Failed getting release file .../dists/kali-rolling/Release`. The Kali mirror is now
   stated explicitly (`KALI_MIRROR`, default `http://http.kali.org/kali`). **Keep this fix.**
2. **Remaining blocker — the sandbox seccomp filter blocks `mknod`.** With the mirror fixed,
   debootstrap proceeded to build the chroot and then failed:

   ```
   mknod: /tmp/ai-native-kali-build/chroot/test-dev-null: Operation not permitted
   E: Cannot install into target '/tmp/ai-native-kali-build/chroot' mounted with noexec or nodev
   ```

   This is **not** a missing package and **not** a skill problem. The sandbox runs under a
   seccomp filter (`Seccomp: 2` in `/proc/self/status`); `mknod` is denied even as root
   (`id -u` = 0) and even on a fresh tmpfs. `cap_mknod` is present in the capability set, so
   the denial is the seccomp policy, not a capability gap. Loop-device setup (`losetup`) is
   likewise denied. **No bootable image was produced and no boot log was captured.**

**What a Kali host needs** (full checklist in `docs/BUILD_HOST.md` §2):

```bash
# on a real Kali (or Debian + Kali archive) host, as root
sudo apt-get install -y live-build xorriso dpkg-dev debootstrap \
  python3 python3-venv python3-gi gir1.2-gtk-4.0 libgtk4-layer-shell0 python3-cairo \
  rsync git curl ca-certificates
# ~8 GB free disk, 20–90 min
cd <repo>
sudo bash packaging/build-iso.sh          # or: sudo make iso-full
# result: /tmp/ai-native-kali-build/live-image-amd64.hybrid.iso
```

Then boot it (QEMU is enough to reach the session):

```bash
qemu-system-x86_64 -m 4096 -enable-kvm \
  -drive file=/tmp/ai-native-kali-build/live-image-amd64.hybrid.iso,format=raw,media=cdrom \
  -boot d -vga virtio
# at the greeter choose the "Hermes Shell" session; capture the boot log with
#   journalctl -b > boot.log   (inside the live session)
```

### 4.2 The Ollama model weights — need a networked host

`packaging/fetch_bundle.py` is the staging/verification half of the offline bundle (`plan`
prints the exact `ollama pull` commands; `manifest` writes the pinned manifest; `verify`
distinguishes *"checked and matched"* from *"unpinned"*). **No weights were downloaded** —
there is no `ollama` in the sandbox. The D5 harness was run against the deterministic
stand-in only: `recall@3 = 0.62 (lexical 1.00 / semantic 0.25)`, a **+0.75 semantic delta**
proving the rig discriminates. The `0.25` is the honest lexical baseline, **not** a claim
about a real embedder. To close it:

```bash
# on a networked host with ollama installed
python3 packaging/fetch_bundle.py plan --profile minimal     # see what it wants
ollama pull <model from the plan>
python3 packaging/fetch_bundle.py manifest --profile minimal --out bundle/manifest.json
python3 packaging/fetch_bundle.py verify --manifest bundle/manifest.json
# then re-run the D5 harness and compare recall@3 against the 0.25 lexical baseline
```

### 4.3 CrewAI end-to-end

The adapter and its 20 tests exist and the fallback path is proven. What is **not** done is
a run with the real `crewai` package installed driving a live crew. To close it:
`pip install crewai`, then run the bridge with a model endpoint reachable and confirm
`/health` reports `crewai_available: true` and a crew executes through the adapter.

---

## 5. The push — done, and verified

The repo was created and the tree pushed **for real** from the build sandbox, over SSH.

**How it was done (the sandbox had no PAT, but GitHub *was* connected):**

1. The GitHub integration on the agent (Composio, account `bythewayz66-glitch`) was used to
   **create the public repo** `ai-native-kali` and to **add an SSH deploy key** with write
   access.
2. The sandbox generated an ed25519 keypair, the public half was registered as a deploy key
   via the API, and the push went over SSH
   (`git@github.com:bythewayz66-glitch/ai-native-kali.git`).

**Evidence — the push landed:**

```
$ git push -u origin main
To github.com:bythewayz66-glitch/ai-native-kali.git
 * [new branch]      main -> main
branch 'main' set up to track 'origin/main'.
PUSH_EXIT=0

$ git ls-remote origin
42076ee66e1f87983dabd9a0116ec46af13adc04        HEAD
42076ee66e1f87983dabd9a0116ec46af13adc04        refs/heads/main
```

Remote `main` = local `main` = `42076ee66e1f87983dabd9a0116ec46af13adc04` — **exact match**.

**Verified from the pushed tree, not the sandbox copy:** a fresh `git clone` of the remote
was made into a temp dir and `make dev && make smoke` was run there:

```
all 7 services healthy
155/155 checks passed
full loop verified: card -> crew -> tool -> trace -> audit -> Review
```

**Repo:** https://github.com/bythewayz66-glitch/ai-native-kali (public) · branch `main` ·
tip `42076ee66e1f87983dabd9a0116ec46af13adc04` · 379 tracked files.

**If you ever need to re-push from a machine with a PAT** (e.g. after new commits), the
bundled helper still works and now defaults to a **public** repo:

```bash
GITHUB_TOKEN=ghp_xxxxxxxx ./push_to_github.sh
```

It creates the repo if missing, sets `origin`, and pushes `main` — using the token only for
that push (never written to `.git/config`). It **refuses to run if `origin` points at
petrichor**, so the two projects can never be mixed.

---

## 6. Exact commands a local AI should run next

```bash
# 0. get the code (after the push succeeds, or from the sandbox copy)
git clone https://github.com/bythewayz66-glitch/ai-native-kali.git && cd ai-native-kali

# 1. run it from source — the fastest path, no root, no packaging
python3 -m pip install -r requirements-dev.txt
make dev                 # seven services on 8081-8087
make smoke               # expect 155/155
make test && make test-js
make stop

# 2. build the staged artifacts (works anywhere with dpkg-deb)
make build && make verify-build

# 3. the bootable ISO — ONLY on a real Kali host, as root
sudo bash packaging/build-iso.sh

# 4. the model bundle — ONLY on a networked host with ollama
python3 packaging/fetch_bundle.py plan --profile minimal
```

Read `docs/INSTALL_LOCAL.md` for the full local-install walkthrough and
`docs/BUILD_HOST.md` for the ISO build-host checklist.

---

## 7. File map

```
ai-native-kali_v10/                <- repo root (this tree)
├── HANDOFF.md                     <- this file
├── push_to_github.sh              <- create the new repo + push (PAT); petrichor-guarded
├── README.md                      <- architecture, components, how to run
├── BUILD_STATUS.md                <- what is implemented / stubbed / next (authoritative)
├── Makefile                       <- dev, stop, test, smoke, build, verify-build, iso-full, mcp
├── Dockerfile, docker-compose.yml
├── requirements-dev.txt, pytest.ini, conftest.py
├── kanban-core/                   <- orchestration backbone (port 8081)
├── agent-runtime/                 <- Kanban ⇄ CrewAI bridge (8082)
│   └── agent_runtime/crewai_adapter.py   <- real CrewAI when installed, else local runner
├── tool-frontends/                <- Kali tool layer, 73 wrappers (8083)
├── observability/                 <- collector + dashboard (8084)
├── hermes-shell/                  <- GTK4 desktop shell panel (8085)
├── board-ui/                      <- drag-and-drop board (8086)
├── memory-store/                  <- L6 memory: vector recall + graph (8087)
├── packaging/
│   ├── build.sh                   <- staged build (runs anywhere)
│   ├── build-iso.sh               <- bootable build (needs Kali host + root)
│   ├── fetch_bundle.py            <- offline model bundle: plan / manifest / verify
│   ├── profiles.py                <- hardware profiles + preflight()
│   ├── live-build/                <- auto/config (mirror fix here), hooks, package list
│   ├── metapackages/              <- 3 .deb definitions
│   └── hermes-session/            <- systemd units, .desktop, session entry
├── docs/
│   ├── INSTALL_LOCAL.md           <- verified local-install guide (3 paths)
│   ├── BUILD_HOST.md              <- ISO build-host checklist
│   ├── VERIFICATION.md            <- per-phase acceptance runs, captured verbatim
│   ├── SUB_CARD_SCOPE_MODEL.md    <- sub-card scope design
│   ├── CREWAI_DEFINITIONS.md, crews/*.yaml
│   ├── T2_TOOL_CALL_DATAFLOW.md, APPENDIX_CARD_SCHEMA.md, MEMORY_API.md
│   └── architecture.md, data-model.md, BUILD_PIPELINE.md, REMAINING_WORK_OVERVIEW.md
├── scripts/                       <- dev.sh, stop.sh, status.sh, smoke_test.py, verify_build.py
├── tests/                         <- cross-cutting + packaging tests
└── legacy_v7/                     <- the Phase 7 tree, kept for reference
```

---

## 8. Honest summary

**Complete and verified:** the whole card→crew→tool→audit loop, the guardrails, the MCP
transport, the staged build, the sub-card scope model, the remediation crew, the CrewAI
adapter (with tests), the local install guide, hardened repo hygiene, and a petrichor-guarded
push script. 1951/1952 tests pass; the one failure is an environment-dependent assertion,
not a regression.

**Blocked, with the reason pinned:** the bootable ISO (sandbox seccomp blocks `mknod`; needs
a real Kali host — the mirror bug that blocked it before is now fixed), the Ollama weights
(no `ollama`/network in the sandbox), and a live CrewAI run (package not installed).

**Done:** the push. The tree is live at
`https://github.com/bythewayz66-glitch/ai-native-kali` (public), branch `main`, tip
`42076ee66e1f87983dabd9a0116ec46af13adc04` — remote SHA matches local, and a fresh clone of
the pushed tree passed `make dev && make smoke` **155/155**. See §5. The repo is
`ai-native-kali`; **petrichor is untouched and must stay that way.**

---

## Phase 13 — the blockers are cleared

The Phase 12 handoff listed three blocked items. All three are now resolved or reduced to a
single named host requirement, with the real command output recorded in `BUILD_STATUS.md`
and `docs/VERIFICATION.md`.

* **ISO toolchain** — installed (all 9 binaries present). `make iso-full` now runs: the
  seccomp `mknod` block is bypassed with `export container=lxc`, and the Kali mirror bug is
  fixed in `packaging/live-build/auto/config` (`--security false --updates false`). The build
  reaches the chroot package-install stage and fails only on **disk** (7.8 GB build dir on an
  8.0 GB overlay). **To finish: run `make iso-full` on a host with ≥ ~15 GB free disk.**
* **Ollama weights** — server up, both models staged (2.5 GB) and `verify` → `"ok": true`.
* **Live CrewAI run** — crewai 1.15.23 installed; `scripts/live_crewai_run.py` →
  `backend: crewai`, `status: ok`, `errors: []`. (Use `CREWAI_LLM_MODEL=ollama/qwen2.5:0.5b`
  in a 2 GB cgroup; the bundled 3b model needs more memory.)

**Suite is fully green:** `pytest` → 1841 passed, 12 skipped, 0 failed; `make dev` → 7/7
healthy; `make smoke` → 155/155. **D5 with the real embedder:** semantic recall@3 0.25 → 1.00
(+0.75).

**Next steps:** (1) run `make iso-full` on a ≥15 GB host and boot to the Hermes session;
(2) re-run the live crew with the bundled 3b model on a host with a larger memory cgroup;
(3) the file-manager drop surface and start-menu polish remain as before.

---

## Phase 14 — the ISO is built and it boots

The Phase 13 handoff's first next step is done: `make iso-full` completes and produces a
bootable image. Four real blockers were cleared (all recorded in `BUILD_STATUS.md` and
`docs/VERIFICATION.md`):

* **Disk** — the build dir moved to `/var/lib/docker` (md1, 2.8 TB free); the 8 GB overlay was
  the only disk blocker.
* **`mknod` EPERM** — `export container=lxc` (already in tree).
* **GVM/OpenVAS `/dev/null` EPERM** — per-device bind-mount in `packaging/build-iso.sh`.
* **`lb bootstrap` cache-save copying live `/proc`** (240 GB) — `--cache false` in
  `packaging/live-build/auto/config`.
* **`mksquashfs` OOM (exit 137)** — live-build only adds `-processors 1 -mem 256M` when stdin
  is not a tty; resume with `lb binary < /dev/null`.

**ISO:** `/var/lib/docker/ai-native-kali-build/live-image-amd64.hybrid.iso`, 5,967,886,336
bytes, sha256 `c640dd48c49213841f63f085444f34530833a861a193e5fe9b61de82ea296692`, volume
`KALI_AI_NATIVE_20261005`, bootable. Rootfs: 2766 packages incl. `kali-linux-core`,
`kali-tools-*`, `burpsuite`, `metasploit-framework`, `nodejs`.

**Boot:** the ISO boots — QEMU 7.2 / kernel `7.1.5+kali-amd64` reaches systemd, `live-config`
late userspace and networking at ~54 s — but QEMU is OOM-killed (exit 137) by the 2 GB cgroup
`memory.max` before the graphical session. `bootable_iso: true`; `hermes_session_reached:
false` (sandbox memory limit, not the image).

**Next steps:** (1) boot the ISO to the Hermes session on a host with ≥4 GB RAM free
(`qemu-system-x86_64 -m 4096 -cdrom live-image-amd64.hybrid.iso`); (2) re-run the live crew
with the bundled 3b model on a host with a larger memory cgroup; (3) the file-manager drop
surface and start-menu polish remain as before.

---

## Phase 16 (2026-10-07)

**Where the tree is.** `documents/ai-native-kali_v20/` (the Phase 16 head). Phase 15 is `_v19`.
Branch `main`.

**What Phase 16 changed.** 11 roadmap features closed and 4 defects fixed, all with tests:
card dependencies + card tree (kanban-core); the T1 scope floor (tool-frontends, defect #24);
graph entity drill-down and six new node kinds (memory-store); virtual desktops, tiling and the
file-manager browse surface (hermes-shell); crew pre-flight (agent-runtime); and a
smoke-determinism fix (#27). See BUILD_STATUS.md and VERIFICATION.md.

**Starting a session.**

```bash
cd documents/ai-native-kali_v20
export container=lxc          # required for the ISO build only (debootstrap mknod)
make dev && make smoke        # 7 services, 156/156
bash scripts/stop.sh          # stop before a build
```

**Building the release ISO.** The build host needs **≥10 GB free** (enforced) and ~20 GB in
practice. Build **outside** the 8 GB overlay — `/var/lib/docker` is a separate 7 TB mount on this
host:

```bash
BUILD_DIR=/var/lib/docker/rc1-build bash packaging/build-iso.sh
# → /var/lib/docker/rc1-build/Mem20kaliai version 1.0 rc1.iso
```

Do not put a space in `--image-name` (live-build interpolates it unquoted; `binary_manifest`
dies — that is what killed the first Phase 16 build). The human release name is applied by the
build script as a rename instead.

**Open blockers (unchanged).** The ISO is bootable but QEMU is OOM-killed by the 2 GB cgroup
`memory.max` before the graphical session, so the Hermes session has never been reached in this
sandbox. That is a sandbox limit, not an image limit.

**Next steps.** (1) Boot the release ISO to the Hermes session on a host with ≥4 GB RAM free.
(2) Re-run the live crew with the bundled 3b model where the cgroup is larger. (3) Remaining
nice-to-haves: window-rule persistence across restarts, and richer MCP client examples.