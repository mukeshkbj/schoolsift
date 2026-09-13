# SchoolSift

**Every school email, turned into the next right action.**

SchoolSift reads school email for busy parents and turns each confirmed
message into an **Action Packet**: a concise summary, the child and deadline
it concerns, the evidence behind every claim, and a small set of reviewable
proposals — a draft reply, a calendar event, or a fillable-PDF form. Nothing
is sent or scheduled without explicit caregiver approval, and payments or
signatures are escalated to a human, never executed.

This repository currently contains the **local demo slice**: a fully
synthetic end-to-end walkthrough of that loop. All messages, people,
schools, and outcomes are fictional and never leave your machine.

## Quick start

Requires Node 24+, pnpm 10.14+, and [uv](https://docs.astral.sh/uv/)
(manages Python 3.12).

```bash
pnpm install
uv sync

# terminal 1 — API on http://127.0.0.1:8000
uv run uvicorn schoolsift.api:create_app --factory --port 8000

# terminal 2 — web on http://localhost:3210
pnpm --dir apps/web dev
```

Open http://localhost:3210, choose **Try the demo**, then **Process demo
inbox**. Approving a proposal writes to a labeled demo outbox/calendar —
no real email is ever sent.

## Verification

```bash
pnpm verify   # lint, typecheck, unit tests, backend checks, build, e2e
```

Individual commands: `pnpm lint`, `pnpm typecheck`, `pnpm test`,
`pnpm test:e2e` (Playwright starts both dev servers itself).

## Architecture (demo slice)

```text
apps/web (Next.js, port 3210)
  └─ fetch → FastAPI (port 8000)
        ├─ demo/messages fixtures + attachments
        ├─ processor.py  deterministic local processor
        ├─ domain.py     Action Packet contract + proposal state machine
        └─ demo_store.py in-memory packets, versions, demo outbox
```

`backend/src/schoolsift/agent.py` defines the real Strands agent seam —
read-only tools, an untrusted-content system prompt, and Pydantic
structured output. The Strands SDK is a regular dependency, so plain
`uv sync` runs every local test; the Bedrock model path is only exercised
inside `build_agent`, which nothing local calls. Wiring it to Amazon
Bedrock AgentCore Runtime is Phase 4 and intentionally not done here.

## Demo vs production

| Capability | This slice | Production (planned) |
|---|---|---|
| Messages | 6 synthetic fixtures | Gmail/Outlook webhooks |
| Processing | Deterministic local processor + Strands seam | Strands on AgentCore Runtime |
| Approval | Versioned, hash-bound demo writes | Real send/calendar after approval |
| Payments/signatures | Escalated, never executed | Same — never executed |

See `AGENTS.md` for contributor/agent rules and the verification contract.

## License

MIT — see `LICENSE`.
