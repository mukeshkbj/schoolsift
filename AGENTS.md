# SchoolSift — agent instructions

Every school email, turned into the next right action.

## What this repo is

A local, fully synthetic demo slice of SchoolSift: a Python (FastAPI) backend
that turns demo school messages into reviewable **Action Packets**, and a
Next.js web app where a caregiver edits, approves, or rejects each proposal.
Approvals write to a clearly labeled **demo outbox/calendar** only — nothing
leaves the machine. Live Strands/AgentCore deployment is Phase 4 and is not
wired up here.

## Safety rules (non-negotiable)

- Demo data only. Never load real provider credentials, never send email,
  never create real calendar events, never accept arbitrary recipients or
  prompts. All fixture people, schools, and addresses are fictional.
- Do not provision AWS resources, modify Google/Microsoft app registrations,
  publish, or deploy. Those steps require explicit approval first.
- Do not commit secrets. `.env` files are gitignored; `scripts/verify_no_secrets.py`
  must pass.

## Layout

- `apps/web` — Next.js 15 / React 19 / TypeScript app (port 3210). Plain CSS.
- `backend/src/schoolsift` — Python package: `domain.py` (contracts +
  proposal state machine), `processor.py` (deterministic local processor),
  `demo_store.py` (in-memory demo persistence), `api.py` (FastAPI app),
  `agent.py` (lazy Strands seam; see its docstring).
- `backend/tests` — pytest suite (domain, processor, API).
- `demo/` — synthetic fixtures: `messages/`, `attachments/`, `expected/`.
- `scripts/` — verification helpers.

## Commands (run from repo root)

```bash
pnpm install          # web deps (frozen lockfile in CI)
uv sync               # Python 3.12 env, dev group
pnpm lint             # web ESLint + ruff check
pnpm typecheck        # tsc --noEmit + mypy strict
pnpm test             # vitest + pytest
pnpm test:e2e         # Playwright (starts web :3210 + api :8000 itself)
pnpm verify           # all of the above + ruff format check + next build
pnpm --dir apps/web dev            # web dev server on :3210
uv run uvicorn schoolsift.api:create_app --factory --port 8000
```

The Strands SDK is a default dependency — plain `uv sync` runs every local
test including the agent-tool suite. Only `build_agent`'s Bedrock model path
would need AWS credentials, and nothing local calls it.

## Conventions

- Proposal mutations are conditional: callers send `expected_version` /
  `payload_hash`; stale requests get HTTP 409 with `{error:{code,message}}`.
- Edits create a new immutable version and supersede the old one.
- Escalation proposals can never be approved.
- Backend style: Ruff (`E,F,I,UP,B,ASYNC,S,C4,SIM,RUF`), mypy strict.
  `datetime`s are timezone-aware UTC.
- Frontend: no Tailwind/component library — plain `globals.css` with the
  documented palette. Controls name their consequence.
