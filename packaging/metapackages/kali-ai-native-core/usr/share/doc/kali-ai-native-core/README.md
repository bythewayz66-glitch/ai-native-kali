# kali-ai-native-core

The orchestration backbone of AI-native Kali: **Hermes Kanban**, the guardrailed
tool frontend layer, the observability collector and the memory store.

Every unit of work on the system - agent task, security engagement, system job
or user request - is a card on a board. The board is simultaneously the task
queue, the permission model, the audit trail and the human control plane.

## Services

| Unit | Component | Port |
|---|---|---|
| `kali-ai-kanban.service` | kanban-core | 8081 |
| `kali-ai-tools` | tool-frontends | 8083 |
| `kali-ai-observability.service` | observability | 8084 |
| `kali-ai-memory` | memory-store | 8087 |

## Safety defaults

- **Dry-run is the default.** A tool call in dry-run mode renders the command and
executes nothing.
- Live execution requires **both** `TOOLS_LIVE=1` and the tool named in
`TOOLS_UNLOCK`. A refused live request returns an explicit denial - it is never
silently downgraded to a dry run.
- Scope is enforced at **two** layers: the bridge (pre-claim) and the tool layer.
- Every tool invocation is written to a **hash-chained** audit log.

See `/usr/share/doc/kali-ai-native-core/README.md` in the source tree
(`docs/`) for the full operator documentation.
