# Lark CM/AM Assistant

Internal system for authorised CM/AM Lark message sync and creator/account review. The next planned scope is auditable event-urgency classification, without reply suggestions or automatic replies.

## Current status

- [Message risk priority v1.0](docs/product/message-risk-priority-v1.0.md) records the current P0–P3 business standard and classification-only scope; it overrides conflicting severity/reply requirements in the older PRD/TDD. Classification itself is not implemented yet.
- `backend/` contains the deployed FastAPI service for authorised CM/AM conversation sync and the owner-only content console.
- `docs/architecture/TDD_v2.0.md` defines the approved target architecture; it remains a technical-review draft until its P0 governance decisions are closed.
- `docs/architecture/contact-identity-organization-model-v0.4.md` documents the deployed person, organization, role, and creator-owner model.
- No customer-facing or automatic message sending is included.

## Repository layout

- `backend/` — FastAPI, PostgreSQL migrations, worker, and backend tests.
- `frontend/` — future React/TypeScript workbench.
- `infra/` — Caddy, systemd and deployment assets; operational procedures live in `docs/runbooks/`.
- `docs/` — product requirements, target architecture, current deployed baseline, ADRs, runbooks, and archived documents.

## Engineering rules

1. GitHub is the sole source of truth; production servers deploy a reviewed release and are never edited directly.
2. Secrets, tokens, customer messages, production exports, and backups must never be committed.
3. Message bodies are collected only after the revised OAuth agreement, encrypted at rest, excluded from logs, and visible only to the designated owner through the audited admin page. AI never sends Lark messages; contact classification is an owner-configured, evidence-bound feature.

## Getting started

The backend baseline documents its local setup in [backend/README.md](backend/README.md). The frontend will be scaffolded in a separate pull request after the Phase 0 architecture decisions are recorded in `docs/adr/`.
