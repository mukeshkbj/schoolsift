# SchoolSift — Devpost submission draft

Hackathon: Agents for Humans (Everyday Agents track)
Repository: https://github.com/mukeshkbj/schoolsift
builder.aws post: https://builder.aws.com/content/3JJrjwp1ua3qPA6TGCBDgLQi4S3/agents-for-humans-building-schoolsift-an-agent-that-reads-school-letters-so-parents-dont-miss-the-deadline

## Tagline

Every school email, turned into the next right action.

## Inspiration

Our household gets school mail from a management system that sends a letter
for everything: allergy forms, flu consent, a Paris trip, detention policy
changes, term dates. Each one is a PDF attached to a two-line email. The
information is real and often has a deadline, but it arrives in the same
inbox as newsletters and receipts, so the deadline gets found the day it
passes. We wanted an agent that does the reading and the drafting, and
leaves the deciding to the parent.

## What it does

SchoolSift connects to the Gmail and Outlook inboxes a family uses for
school (as many as they need), and asks the parent to confirm which senders
are the school. Only those senders are read in full. For each school
message the agent produces an Action Packet:

- a short summary and which child it concerns
- the deadline and urgency
- evidence quotes, each labelled with where it came from (the email or a
  named attachment)
- prepared actions: a draft reply to the school, a calendar event, or a
  filled PDF form
- escalations when a person has to act: payments, signatures, portals,
  forms that cannot be filled safely

Nothing is sent or scheduled until a caregiver approves the exact version
they reviewed. Households can have several caregivers with owner, editor,
and viewer roles, so both parents (or a grandparent) see the same queue.

## What we ran on a real inbox

We connected a real Gmail account through Google OAuth, synced 3,000
message headers, trusted the school's sender, and processed nine real
letters. Examples of the output:

- Allergy Information Request: deadline found, urgency soon, draft reply to
  the school ready to approve.
- NHS Flu Immunisations: vaccination day read from the attached PDF letter
  and proposed as a calendar event; the consent request itself was
  escalated because it lives on an external NHS portal.
- 2027 Paris trip (£530): deadline and payment schedule summarised;
  payment never automated; the permission form escalated because the PDF
  has no fillable fields.
- Term 1 letter: return day and time read from the uniform-letter PDF and
  proposed as a calendar event for the right child.
- Detention policy, careers, consents reminders: correctly marked
  information-only with no proposed action.

## How we built it

- Agent: Strands Agents SDK with Pydantic structured output, running on
  Amazon Bedrock (Claude Haiku 4.5 via a cross-region inference profile).
  The agent has read-only tools scoped to the household (children,
  calendar, documents) and receives attachments as Bedrock document blocks.
  Email content is treated as untrusted data; a policy layer validates every
  proposal (recipients limited to the school thread, calendar windows must
  be well formed, PDF fields must exist and may not be payment or signature
  fields) before anything is stored.
- Backend: Python 3.12, FastAPI, SQLite locally with ordered migrations;
  encrypted content store; OAuth tokens in the OS keychain locally and
  Secrets Manager in AWS.
- Ingestion: Gmail history sync and Microsoft Graph delta sync; Gmail
  Pub/Sub and Graph change-notification webhooks that verify and enqueue
  only; SQS FIFO queues with dead-letter queues.
- Execution: approval creates one idempotent execution; a worker sends the
  reply or creates the event with deterministic ids so retries cannot
  double-send; ambiguous outcomes are surfaced as "delivery uncertain"
  rather than retried.
- Cloud: AWS CDK stack (KMS, S3 with 30-day expiry, DynamoDB, SQS, Cognito,
  ECR, CloudWatch alarms, EventBridge Scheduler) deployed to us-east-1, and
  the agent packaged for Amazon Bedrock AgentCore Runtime by direct code
  deployment (aarch64 zip), deployed and READY behind a Cognito JWT
  authorizer.
- Web: Next.js 15, plain CSS, Zod at the API boundary, Playwright and
  Vitest tests.
- Quality: 484 backend tests, 68 web tests, 17 infrastructure assertions,
  strict mypy and TypeScript, secret scan, and validated architecture
  diagrams (Archify) with an editable Excalidraw export.

## Challenges we ran into

- Real school PDFs arrive labelled `application/octet-stream`; we now sniff
  the content instead of trusting the declared type.
- The model drifts on small things (drops a dot in a filename, writes
  "Email body" instead of the exact source label, omits a time zone). We
  normalise those instead of failing the whole message, while keeping the
  hard rules (recipients, payments, signatures) strict.
- A brand-new AWS account needs the Anthropic use-case form and cross-region
  inference profiles before Bedrock will answer; we surface those as clear,
  retryable errors instead of "could not process".
- Multiple accounts per provider do not fit the AgentCore Identity vault's
  one-user-one-provider model, so credentials live per connected inbox in
  Secrets Manager and the agent never sees a token.

## Accomplishments we're proud of

A parent can connect a real inbox and get real, evidence-backed packets
from real school letters, with the agent refusing to do the things it
should not do (pay, sign, fill a form it cannot verify) and saying so.

## What's next

- Wire the DynamoDB store and host the API and workers in AWS so the
  deployed AgentCore runtime processes messages instead of the local agent.
- First live approved send and calendar insert on the real account.
- Outlook end to end, Gmail push notifications, invitation emails, digests.
- Google restricted-scope verification so families beyond our test users
  can connect.

## Built with

Python, FastAPI, Strands Agents, Amazon Bedrock, Amazon Bedrock AgentCore
Runtime, AWS CDK, DynamoDB, S3, SQS, Cognito, KMS, Next.js, TypeScript,
SQLite, Gmail API, Microsoft Graph, pypdf, httpx, Playwright, Vitest.

## Demo script (3 minutes)

1. Landing page: the promise and the three steps (15 s).
2. Accounts: a real Gmail connected; press Sync (15 s).
3. Senders: 549 senders grouped by domain; search "thamesview", Trust all
   in domain (20 s).
4. To review: the queue of real school letters, grouped into "Needs a
   decision" and "For your information" (15 s).
5. Open NHS Flu Immunisations: summary, child, deadline in local time,
   evidence chips showing the PDF was read, calendar proposal from the PDF,
   escalation for the portal (45 s).
6. Open Allergy Request: edit the draft reply, approve; show the execution
   record and the confirmation gate before anything is sent (30 s).
7. Paris trip: £530 payment summarised, never automated; unfillable form
   escalated (20 s).
8. Settings: children, caregivers with roles, invite (15 s).
9. Architecture diagram and the AWS console: deployed stack and AgentCore
   runtime READY (15 s).
