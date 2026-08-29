# Lark CM/AM Assistant

Internal system for managing CM/AM night-time Lark messages, generating auditable priority events, and providing internal AI reply suggestions.

## Current status

- `backend/` contains the deployed v0.1 message-statistics prototype as the migration baseline.
- `docs/architecture/TDD_v2.0.md` defines the approved target architecture; it remains a technical-review draft until its P0 governance decisions are closed.
- No customer-facing or automatic message sending is included.

## Repository layout

- `backend/` — FastAPI, PostgreSQL migrations, worker, and backend tests.
- `frontend/` — future React/TypeScript workbench.
- `infra/` — Caddy, systemd, deployment, backup, and smoke-test assets.
- `docs/` — product requirements, target architecture, current deployed baseline, ADRs, runbooks, and archived documents.

## Engineering rules

1. GitHub is the sole source of truth; production servers deploy a reviewed release and are never edited directly.
2. Secrets, tokens, customer messages, production exports, and backups must never be committed.
3. The production message-body and AI features stay disabled until the required consent, data-governance, and security decisions are implemented.

## Getting started

The backend baseline documents its local setup in [backend/README.md](backend/README.md). The frontend will be scaffolded in a separate pull request after the Phase 0 architecture decisions are recorded in `docs/adr/`.
