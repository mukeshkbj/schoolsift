# SchoolSift — agent instructions

Every school email, turned into the next right action.

## What this repo is

The production foundation of SchoolSift: a Python (FastAPI) backend that
turns confirmed school messages into reviewable **Action Packets**, and a
Next.js web app where a caregiver edits, approves, or rejects each proposal.
Local mode persists to a SQLite database that starts **empty** — no seed
data, no fake provider results. Approving a proposal creates an idempotent
execution record and outbox entry; a real provider call only happens when a
worker (or the explicit local run control) executes it. Live
Strands/AgentCore deployment is a later phase and is not wired up here.

## Safety rules (non-negotiable)

- No synthetic data in the running app. Sanitized fixtures live only under
  `backend/tests/` and `apps/web/tests/`; runtime code never imports them.
- Never load real provider credentials, never send email, never create real
  calendar events, never accept arbitrary recipients or prompts.
- Do not provision AWS resources, modify Google/Microsoft app registrations,
  publish, or deploy. Those steps require explicit approval first.
- Do not commit secrets. `.env` files and `.schoolsift/` are gitignored;
  `scripts/verify_no_secrets.py` must pass.

## Layout

- `apps/web` — Next.js 15 / React 19 / TypeScript app (port 3210). Plain CSS.
  `lib/contracts.ts` holds Zod schemas for every API boundary.
- `backend/src/schoolsift` — Python package: `domain.py` (contracts +
  proposal state machine), `models.py` (persisted records), `config.py`
  (env-driven `Settings`; `aws` fails closed without full config),
  `store.py` (`SchoolSiftStore` protocol), `sqlite_store.py` (stdlib
  sqlite3, WAL, `BEGIN IMMEDIATE` proposal mutations, per-operation
  connections, ordered schema migrations), `providers.py` (Gmail/Outlook
  OAuth + mail over injected httpx), `credentials.py` (`CredentialVault` —
  keychain `KeyringCredentialVault`), `content.py` (Fernet-encrypted
  content blobs keyed by opaque ref), `sync.py` (`sync_connection`
  transaction script), `documents.py` (`read_document` — bounded
  text/office/PDF extraction with typed unsupported-document errors),
  `agent_data.py` (`AgentDataSource` + `StoredAgentDataSource` resolving
  encrypted refs), `agent.py` (`MessageAnalyzer` + lazy
  `StrandsMessageAnalyzer`), `processing.py` (`process_message` validates
  analyzer output and persists Action Packets), `notifications.py`
  (`IngestionEvent` + durable `IngestionQueue`/`SQLiteIngestionQueue` +
  `process_ingestion_event` worker — no forever-loop runs yet),
  `webhooks.py` (`GooglePushVerifier` OIDC boundary + Pub/Sub/Graph wire
  models), `subscription_service.py` (provider watch/subscription create
  and renewal), `workers.py` (pure `create_ingestion_handler` /
  `create_renewal_handler` / `create_execution_handler` Lambda-compatible
  factories — dependency
  injection only, no module-level AWS clients, nothing deployed yet),
  `identity.py` (`Principal`, `TokenVerifier`,
  `CognitoIdTokenVerifier` — RS256 ID-token verification against an
  injected key provider; `LOCAL_PRINCIPAL` is the fixed local-mode
  identity), `proposal_policy.py` (shared proposal safety rules for model
  output and caregiver edits), `api.py`
  (`create_app(settings, store, providers, vault, content, analyzer,
  ingestion_queue, google_push_verifier, token_verifier)` — all
  injectable).
- `backend/src/schoolsift/aws_adapters.py` — injected-client AWS
  adapters: `S3ContentStore` (KMS-encrypted blobs with SHA-256 integrity
  metadata), `SecretsManagerCredentialVault` (revoke-by-marker, never
  force-deletes), `SQSIngestionQueue` (FIFO, hashed dedupe). None are
  wired into the running app — they exist for the future AWS deployment
  and have never made a live AWS call. Note: `SQSIngestionQueue.enqueue`
  returns `True` when SQS accepts the send; SQS FIFO dedupe suppression
  is invisible to the sender, so a suppressed duplicate also reports
  `True` (unlike the SQLite queue, which reports dedupe as `False`).
- `backend/src/schoolsift/aws_state.py` — `DynamoStateRepository`, a
  bounded household-scoped state primitive (PK=`HOUSEHOLD#…`,
  SK=`<TYPE>#…`, versioned conditional writes, global `CLAIM#<sha256>`
  items that enforce uniqueness via `attribute_not_exists` inside
  `transact`, GSI1 fields for lookup acceleration only, and
  HMAC-authenticated cursors bound to household + entity type). It is
  **not** a `SchoolSiftStore`
  implementation; the request-scoped Cognito/membership store that would
  build on it does not exist yet.
- `backend/src/schoolsift/agentcore_app.py` — `BedrockAgentCoreApp`
  entrypoint foundation: strict `{household_id, message_id}` input, no
  arbitrary prompts, returns an `ActionPacketDraft` only (no persistence,
  no approval, no side effects). It fails closed until
  `SCHOOLSIFT_AGENT_DATA_FACTORY` (a `module:callable` spec) plus
  `SCHOOLSIFT_BEDROCK_MODEL_ID`/`SCHOOLSIFT_AWS_REGION` are configured.
  `backend/agentcore_entry.py` is the deployment-zip launcher (CFN
  EntryPoint allows at most two argv elements, so `python -m` cannot be
  expressed — the zip root must contain `agentcore_entry.py`).
- `infra/` — AWS CDK v2 stack (`SchoolSiftStack`): CMK, private
  versioned S3 content bucket, DynamoDB state table (GSI1/GSI2, PITR,
  TTL), FIFO ingestion/execution queues + DLQs, Cognito pool + PKCE
  client, ECR repos, log groups, queue/DLQ alarms, disabled-by-default
  EventBridge Scheduler schedules, and an L1 `CfnRuntime` AgentCore
  resource with a Cognito JWT authorizer. It synthesizes **offline**
  (`pnpm infra:synth`) and is a foundation only — it is not end-to-end
  deployable: no workers, no subscription renewal/retention handlers, no
  API service, and no AgentCore data-source factory are implemented yet.
- `backend/tests` — pytest suite (domain, store, API, config, agent
  tools, AWS adapter/state fakes, agentcore entrypoint).

## Commands (run from repo root)

```bash
pnpm install          # web deps (frozen lockfile in CI)
uv sync               # Python 3.12 env, dev group
pnpm lint             # web ESLint + ruff check
pnpm typecheck        # tsc --noEmit + mypy strict
pnpm test             # vitest + pytest
pnpm test:e2e         # Playwright (starts web :3210 + api :8000 itself)
pnpm infra:test       # CDK assertions + infra tsc (offline)
pnpm infra:synth      # offline `cdk synth` — no AWS calls or account lookups
pnpm verify           # all of the above + ruff format check + next build
pnpm --dir apps/web dev            # web dev server on :3210
uv run uvicorn schoolsift.api:create_app --factory --port 8000
```

The Strands SDK is a default dependency — plain `uv sync` runs every local
test including the agent-tool suite. Only `build_agent`'s Bedrock model path
would need AWS credentials, and nothing local calls it.

## Conventions

- Every store read/write is household-scoped; cross-tenant IDs fail closed.
- Request identity: `SCHOOLSIFT_AUTH_MODE=local` uses the fixed
  `LOCAL_PRINCIPAL` without a token (single-caregiver local use only);
  `cognito` requires `Authorization: Bearer <id_token>` verified against
  issuer + app client audience. AWS environment fails closed unless
  cognito mode is configured. Public routes: `/health`, provider OAuth
  callbacks, and the Gmail/Outlook webhooks only.
- Multi-caregiver households: `memberships` (owner/editor/viewer) and
  `invitations` tables (migration v8). `X-SchoolSift-Household` selects
  the active household; membership is verified server-side on every
  household route. Viewers are read-only; member management is
  owner-only; a household always keeps at least one owner. Invitation
  tokens are stored as SHA-256, single-use, email-bound, 7-day expiry;
  the plaintext is returned exactly once at creation — no invite email is
  sent yet.
- OAuth state is 32-byte random, stored only as SHA-256, 10-minute TTL,
  single-use. Tokens live in the OS keychain; provider error bodies never
  reach clients.
- Sync imports bodies/attachments only for confirmed senders; content is
  Fernet-encrypted under the content directory and referenced by opaque
  IDs — raw content never touches SQLite or API responses.
- Webhook endpoints (`POST /v1/webhooks/gmail|outlook`) only validate,
  authenticate, and enqueue `IngestionEvent`s into the durable SQLite
  queue — they never sync or call the agent inline. Gmail pushes require
  an OIDC bearer token verified against the configured audience and
  service account; Outlook notifications are accepted only for stored
  subscriptions with a matching clientState hash. Raw webhook bodies,
  bearer tokens, and clientState values are never persisted or echoed.
  Webhook `provider_cursor` values are high-water evidence only — workers
  always resume from the stored subscription cursor, never from the
  webhook's historyId or resource id.
- Sync cursors are typed and provider-prefixed: Gmail uses
  `page:<token>` (resumable page position), `history:<id>` (durable watch
  checkpoint), and `page-history:<startId>:<token>`; Outlook cursors are
  the exact validated Graph v1.0 next/delta URLs — hostile URLs are
  rejected. `MessagePage.has_more` distinguishes pagination from the
  durable checkpoint; repeated cursors inside one run fail closed. Gmail
  history 404 and Graph 410 map to `FullResyncRequiredError` and trigger
  exactly one bounded 30-day resync.
- Notification lifecycle: `POST /v1/connections/{id}/notifications`
  (editor/owner) registers Gmail `users/me/watch` (INBOX, include) or a
  Graph `/subscriptions` created-change subscription (≤2d23h expiry,
  clientState stored only as SHA-256). Renewal thresholds: Gmail expiry
  <24h or stale >24h; Outlook expiry <12h or `renewal_due`. Local mode
  returns 503 until an HTTPS `SCHOOLSIFT_PUBLIC_API_URL` and
  `SCHOOLSIFT_GMAIL_TOPIC` are configured; OAuth callback never
  auto-registers. Provider subscription IDs, cursors, and clientState
  hashes never leave the API.
- Approving a proposal is the trigger for an external action: it atomically
  marks the proposal approved and creates exactly one `executions` row plus
  one `execution_outbox` row (migration v9). Re-approval is idempotent and
  returns the same execution; rejecting creates nothing; escalations stay
  non-approvable. Execution statuses: `pending_dispatch`, `queued`,
  `executing`, `completed`, `failed`, `delivery_uncertain`. Transport
  errors after an irreversible call (anything but a refused connection,
  which provably never left) settle terminally as `delivery_uncertain` —
  ambiguous sends/events are never auto-retried.
- `execution.py`: `dispatch_pending_executions` moves outbox rows onto an
  `ExecutionQueue` (dedupe = idempotency key; SQS FIFO adapter in
  `aws_adapters.py` uses `MessageGroupId=household`); `execute_command`
  claims once (attempt counter + lease), reloads and hash-verifies the
  immutable approved payload, refreshes OAuth credentials, then performs the
  reply/calendar/pdf-form operation. PDF forms are filled via pypdf
  `update_page_form_field_values` against existing AcroForm fields only —
  never flattened or signed — stored through `ContentStore`, then emailed.
  `update_execution_checkpoint` persists the Outlook draft id before the
  irreversible send. Local-only `POST /v1/executions/dispatch` (editor/owner)
  and `POST /v1/executions/{id}/run` (requires `{confirm: true}`) exist for
  development; in `aws` mode they 404 and workers own execution.
  `output_ref` is excluded from all API serialization.
- Audit rows (`audit_events`) record approval, dispatch, and terminal
  outcome — metadata only, never bodies, tokens, or provider payloads.
- Proposal mutations are conditional: callers send `expected_version` /
  `payload_hash`; stale requests get HTTP 409 with `{error:{code,message}}`.
- Edits create a new immutable version and supersede the old one.
- Escalation proposals can never be approved.
- Backend style: Ruff (`E,F,I,UP,B,ASYNC,S,C4,SIM,RUF`), mypy strict.
  `datetime`s are timezone-aware UTC.
- Frontend: no Tailwind/component library — plain `globals.css` with the
  documented palette. Controls name their consequence. API responses are
  validated with Zod at the trust boundary; no casts.
