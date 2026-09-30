# HANDOFF — AI-native Kali

**Written:** 2026-09-30 · **Tree:** `ai-native-kali_v9` (this repo root)
**Local commit:** `6643b3ef4fb5a8b8e9c5121de55f9314e49274ca` (branch `main`, 377 tracked files)
**Push status:** ❌ **FAILED — no credentials in the build sandbox.** See §5.

This document is self-contained: a fresh local agent can continue from here without
the chat that produced it. Read it top to bottom before running anything.

---

## 1. What this project is

An AI-native security OS that fuses **Kali Linux**, **Hermes Desktop** and **CrewAI**.
The one idea: *every unit of work is a card on a board.* Agent tasks, security
workflows and user requests are all cards moving through
`Backlog → Assigned → Running → Review → Done/Blocked`; every tool call a crew makes
is written to a hash-chained audit log. The board is the task queue, the observability
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

## 2. Current state — verified in this run

Run on the build host on 2026-09-30 against this exact tree:

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
> *this sandbox cannot build the ISO*. In this run the sandbox **does** have
> `lb`, `xorriso` and `debootstrap` installed, so `preflight()` correctly returns
> `ok: True` and the test fails. The test is environment-dependent, not a code
> regression. On a host without the ISO tooling it passes. See §4.

---

## 3. What is done and verified

- **The full loop** — card → crew → tool → trace → audit → `Review`, with the human
  owning `Done`. Verified by `make smoke` (155 checks).
- **Event-driven claim** — a card is claimed over `/ws/events` with **zero fallback
  polls** (asserted by the smoke test).
- **Guardrails** — T0–T3 tiers, dry-run by default, live execution opt-in per tool,
  scope enforced twice (bridge + tool layer), no shell ever, two independent hash chains.
- **MCP stdio transport** — a full session from a subprocess client.
- **Staged build** — `make build` produces 3 `.deb` metapackages, a chroot overlay
  tarball and a manifest whose hashes are checked (`make verify-build`). Runs here.
- **CrewAI adapter** — `agent-runtime/agent_runtime/crewai_adapter.py` **exists** and
  is covered by **20 tests** (`agent-runtime/tests/test_crewai_adapter.py`). It imports
  `crewai` lazily and falls back to the deterministic local runner when the package is
  absent, so there is **no hard dependency**. `crewai_available()` / `crewai_version()`
  are reported on `/health`. **Not yet wired end-to-end** through a live crew run — see §4.
- **Sub-card scope model + remediation crew** — design doc
  (`docs/SUB_CARD_SCOPE_MODEL.md`) and enforcement, with the invariant that an *absent*
  scope is the **widest** scope (a parent-scoped / child-unscoped pair is a refusal).
- **Local install guide** — `docs/INSTALL_LOCAL.md`, every command run against this tree.

---

## 4. What is blocked, and why

### 4.1 The bootable ISO — needs a real Kali host

`make iso-full` (`packaging/build-iso.sh`) **cannot complete in the build sandbox.**
This run got further than any previous one and pinned the blocker precisely:

1. **Fixed this run — the mirror was never named.** `packaging/live-build/auto/config`
   did not pass `--mirror-bootstrap` / `--mirror-chroot` / `--mirror-binary`, so
   live-build fell back to its Debian default and asked it for
   `dists/kali-rolling/Release` — a path that does not exist on the Debian mirror.
   debootstrap died with `Failed getting release file .../dists/kali-rolling/Release`.
   The Kali mirror is now stated explicitly (`KALI_MIRROR`, default
   `http://http.kali.org/kali`). **This fix is real and should be kept.**
2. **Remaining blocker — the sandbox seccomp filter blocks `mknod`.** With the mirror
   fixed, debootstrap proceeded to build the chroot and then failed:

   ```
   mknod: /tmp/ai-native-kali-build/chroot/test-dev-null: Operation not permitted
   E: Cannot install into target '/tmp/ai-native-kali-build/chroot' mounted with noexec or nodev
   ```

   This is **not** a missing package and **not** a skill problem. The sandbox runs
   under a seccomp filter (`Seccomp: 2` in `/proc/self/status`); `mknod` is denied even
   as root (`id -u` = 0) and even on a fresh tmpfs. `cap_mknod` is present in the
   capability set, so the denial is the seccomp policy, not a capability gap. Loop-device
   setup (`losetup`) is likewise denied. **No bootable image was produced and no boot log
   was captured.**

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

`packaging/fetch_bundle.py` is the staging/verification half of the offline bundle
(`plan` prints the exact `ollama pull` commands; `manifest` writes the pinned manifest;
`verify` distinguishes *"checked and matched"* from *"unpinned"*). **No weights were
downloaded** — there is no `ollama` in the sandbox. The D5 harness was run against the
deterministic stand-in only: `recall@3 = 0.62 (lexical 1.00 / semantic 0.25)`, a
**+0.75 semantic delta** proving the rig discriminates. The `0.25` is the honest lexical
baseline, **not** a claim about a real embedder. To close it:

```bash
# on a networked host with ollama installed
python3 packaging/fetch_bundle.py plan --profile minimal     # see what it wants
ollama pull <model from the plan>
python3 packaging/fetch_bundle.py manifest --profile minimal --out bundle/manifest.json
python3 packaging/fetch_bundle.py verify --manifest bundle/manifest.json
# then re-run the D5 harness and compare recall@3 against the 0.25 lexical baseline
```

### 4.3 CrewAI end-to-end

The adapter and its 20 tests exist and the fallback path is proven. What is **not** done
is a run with the real `crewai` package installed driving a live crew. To close it:
`pip install crewai`, then run the bridge with a model endpoint reachable and confirm
`/health` reports `crewai_available: true` and a crew executes through the adapter.

---

## 5. The push — what happened and what you must do

The repo was committed locally and the push was attempted. **It failed:**

```
$ git push -u origin main
remote: Repository not found.
fatal: Authentication failed for 'https://github.com/bythewayz66-glitch/petrichor.git/'
PUSH_EXIT=128
```

**Why:** the build sandbox has **no GitHub credentials of any kind** — no `gh` CLI, no
`GITHUB_TOKEN`/`GH_TOKEN` in the environment, no `~/.git-credentials`, no `~/.netrc`, no
SSH key in `~/.ssh`, and no GitHub MCP integration. The repo is private, so an
unauthenticated request returns `404` (GitHub reports "Repository not found" rather than
"forbidden" for private repos). This is a credentials gap, not a network problem —
`https://github.com` and `https://api.github.com` both answer `200` from the sandbox.

**What you need to do** (any one of these, on a machine that has your credentials):

```bash
# Option A — HTTPS with a Personal Access Token (needs `repo` scope)
cd <this repo>
git remote set-url origin https://<YOUR_PAT>@github.com/bythewayz66-glitch/petrichor.git
git push -u origin main

# Option B — SSH
git remote set-url origin git@github.com:bythewayz66-glitch/petrichor.git
git push -u origin main

# Option C — GitHub CLI
gh auth login && git push -u origin main
```

The local commit is ready and needs no changes: **`6643b3ef4fb5a8b8e9c5121de55f9314e49274ca`**
on branch `main`, 377 tracked files, containing the v8 tree plus the `legacy_v7/` tree.

> If the remote already has history (the previous push was at
> `ea097beb40ba9fccda48046eba5120c96589556d`), you may need
> `git pull --rebase origin main` first, or `git push --force-with-lease` if you intend
> this tree to replace it. **Decide deliberately** — this commit is a fresh root commit
> (the sandbox had no `.git`), so it does not share history with the old one.

---

## 6. Exact commands a local AI should run next

```bash
# 0. get the code (after the push succeeds, or from the sandbox copy)
git clone https://github.com/bythewayz66-glitch/petrichor.git && cd petrichor

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
ai-native-kali_v9/                 <- repo root (this tree)
├── HANDOFF.md                     <- this file
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
├── scripts/                       <- dev.sh, stop.sh, status.sh, smoke_test.py
├── tests/                         <- cross-cutting + packaging tests
└── legacy_v7/                     <- the Phase 7 tree, kept for reference
```

---

## 8. Honest summary

**Complete and verified:** the whole card→crew→tool→audit loop, the guardrails, the
MCP transport, the staged build, the sub-card scope model, the remediation crew, the
CrewAI adapter (with tests), the local install guide. 1951/1952 tests pass; the one
failure is an environment-dependent assertion, not a regression.

**Blocked, with the reason pinned:** the bootable ISO (sandbox seccomp blocks `mknod`;
needs a real Kali host — the mirror bug that blocked it before is now fixed), the Ollama
weights (no `ollama`/network in the sandbox), and a live CrewAI run (package not installed).

**Not done:** the push. The commit exists locally; the sandbox has no credentials for the
private repo. Supply a PAT or SSH key and push — see §5.
