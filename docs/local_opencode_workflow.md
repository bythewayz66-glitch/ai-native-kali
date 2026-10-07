# Local opencode workflow (post-handoff)

**Item 9 of the Dream list.** The shift to a **local `opencode` workflow once the
handoff is ready**: what changes, why, and the exact loop. This is the operating
document for continuing the project on your own machine after the sandbox work is
handed over.

---

## Why move local

The sandbox is the right place to *build and verify* this system — it has the
services, the test suite and the ISO toolchain. It is the wrong place to *live*
in the code day to day, for three concrete reasons that showed up in this work:

1. **The sandbox session is not durable.** A tmux server died mid-run and cost
   two full attempts ("no server running on /tmp/tmux-0/default"). Work that
   must survive belongs in git, and work that must be interactively edited
   belongs on your machine.
2. **The environment has hard ceilings that are not bugs.** A 2 GB memory cgroup
   killed the guest VM every time the ISO was booted; an 8 GB overlay could not
   hold a live-build tree. A local box with real disk and RAM removes a whole
   class of "blocked" that is really "under-provisioned".
3. **Iteration latency.** Every edit in the sandbox is a round trip. Local
   `opencode` gives you an editor, a shell and the model in one loop.

## Prerequisites

- The repo cloned, **the ISO toolchain installed on the host** (see
  `packaging/build-iso.sh`): `live-build xorriso debootstrap squashfs-tools
  syslinux isolinux mtools dosfstools grub-pc-bin grub-efi-amd64-bin`.
- `container=lxc` in the environment for any chroot/debootstrap step — the
  mknod workaround is a property of the *host*, not the repo, and it applies
  wherever the host denies `mknod`.
- Enough disk for a live build (a chroot image plus the squashfs step; budget
  **≥15 GB** free, and point `BUILD_DIR` at your largest filesystem).
- Python deps from `requirements-dev.txt`, plus `crewai` if you want the live
  crew path.

## The workflow

```
# 1. get the head
git clone git@github.com:bythewayz66-glitch/ai-native-kali.git
cd ai-native-kali
git log --oneline -1          # confirm the SHA you expect from the handoff

# 2. install once
python3 -m pip install -r requirements-dev.txt
python3 -m pip install crewai

# 3. the inner loop
opencode .                    # or: opencode --model <local model>
```

Inside the loop, the four commands worth running before every commit:

```bash
python3 -m pytest -q                       # the full suite; the gate
python3 scripts/scope_boundary_audit.py    # items 5-6 regressions
make dev && make smoke                     # services up, smoke checks green
```

`make dev` starts the seven services on 8081–8087; `make smoke` is the
end-to-end check. **`make smoke` needs the services up** — run it after
`make dev`, not instead of it.

## Where the handoff lives

| File | Read it for |
|---|---|
| `HANDOFF.md` | what is done, what is not, and the exact state of each |
| `BUILD_STATUS.md` | per-component counts and the honest scope statement |
| `docs/VERIFICATION.md` | how each claim was verified, with the commands |
| `docs/REMOTE_BUILD.md` / `packaging/` | building the ISO on a real host |

Start every local session by reading `HANDOFF.md`'s "what remains" section. It is
written to be the single place that answer lives, precisely so a local workflow
does not have to reconstruct it from memory.

## Working rules that carried over

- **One version directory per round.** The sandbox convention was
  `documents/ai-native-kali_vN`; on your machine, use a branch per workstream
  instead (`git switch -c phase15/mcp-stdio`). The rule is the same: never edit
  a tree you have not identified as the head.
- **Assertions must cite command output.** Every number in `BUILD_STATUS.md` and
  `docs/VERIFICATION.md` has the command that produced it. Keep that property or
  the documents stop being load-bearing.
- **`make iso-full` is a host-side operation.** It does not belong in CI and it
  does not belong in a sandbox with a small overlay.
- **The live crew path needs a model that fits your memory.** `qwen2.5:3b` was
  OOM-killed under a 2 GB cgroup; `qwen2.5:0.5b` completed. On a local box, size
  the model to the RAM you have, not to the one the docs mention.

## What "handoff is ready" means

The handoff is ready when, from a clean clone on your machine:

1. `python3 -m pytest -q` is green;
2. `python3 scripts/scope_boundary_audit.py` prints `RESULT: PASS`;
3. `make dev && make smoke` passes;
4. `git log --oneline -1` matches the SHA recorded in `HANDOFF.md`.

Until all four hold on *your* box, treat the sandbox as the reference and the
local clone as a port. After they hold, the local clone is the reference and the
sandbox becomes the place you go to *build an ISO*.
