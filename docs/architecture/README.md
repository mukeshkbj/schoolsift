# Architecture diagrams

Three diagrams describe SchoolSift. Each has a JSON source (the file of
record), a standalone interactive HTML built from it, and a 2048x1320
screenshot (`*.visual-check.2048x1320.light.png`).

| Diagram | Source | Interactive | Notes |
|---|---|---|---|
| Production architecture | `system.architecture.json` | `system.html` | Components, AWS boundary, approval boundary, three guided views |
| Email to approved action | `message-to-action.sequence.json` | `message-to-action.html` | Webhook, sync, analysis, approval, execution as a sequence |
| Action Packet lifecycle | `action-packet.lifecycle.json` | `action-packet.html` | Proposal and execution states, escalations, terminal exits |

`system.excalidraw` is an editable export of the architecture diagram for
[Excalidraw](https://excalidraw.com): open the app, drag the file in, and
edit. Shapes keep their arrow bindings, so moving a box moves its
connections.

## Rebuilding

The HTML files are produced with the Archify CLI (installed as a local
agent skill, not a repository dependency):

```bash
node <archify>/bin/archify.mjs deliver architecture docs/architecture/system.architecture.json docs/architecture/system.html --quality showcase
node <archify>/bin/archify.mjs deliver sequence     docs/architecture/message-to-action.sequence.json docs/architecture/message-to-action.html --quality showcase
node <archify>/bin/archify.mjs deliver lifecycle    docs/architecture/action-packet.lifecycle.json docs/architecture/action-packet.html --quality showcase
node <archify>/bin/archify.mjs visual-check docs/architecture/system.html
```

All three sources pass Archify's `showcase` validation (nine artifact
checks, zero composition errors or warnings). The architecture and
lifecycle HTML also pass the desktop containment check at 1440x900,
1600x1000, 1920x1080, and 2048x1320. The sequence diagram scrolls
vertically at those sizes; its fifteen messages do not fit one screen at
readable type.

The Excalidraw file is generated from the architecture source:

```bash
python3 scripts/archify_to_excalidraw.py docs/architecture/system.architecture.json docs/architecture/system.excalidraw
```

Edit the JSON source, not the HTML or the Excalidraw export, when the
system changes; then rebuild both.
