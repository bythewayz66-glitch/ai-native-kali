# kali-ai-native-agents

The CrewAI agent runtime and the Kanban bridge: cards trigger crews, crews claim
cards and write results back.

## Loop

```
card enters Assigned
  -> bridge claims it (WebSocket event, polling only as a fallback)
  -> bridge reads engagement memory (similarity recall) for the card
  -> card moves to Running
  -> crew runs, calling tools through the guardrailed layer
  -> traces, artifacts and the result are written back to the card
  -> card moves to Review
  -> a human approves or rejects at the gate; Rejected returns it to the crew
```

## Crews

| Crew | Roles | Typical tier |
|---|---|---|
| `recon` | recon-specialist, osint-analyst, reporter | T0-T1 |
| `vuln-assessment` | vulnerability-analyst, exploit-validator, reporter | T2-T3 (gated) |

The model is optional. With a reachable Ollama-compatible endpoint the crew plans
with a real local model; without one it falls back to the deterministic local
runner, so the system is testable with no model and no API key.

## Human-in-the-loop

A card in `Review` with a pending approval **blocks until a human decides**.
Approval is a card state, not a separate queue, so the gate is visible on the
same board as the work. Approval is never inferred from elapsed time.
