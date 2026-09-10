# Lark CM/AM Assistant

Internal system for authorised CM/AM Lark message sync, creator/account review, and an owner-enabled off-hours message summary, organised by P0–P3 urgency, without reply suggestions or automatic replies.

## Current status

- [Off-hours message summary — plain-language requirements](docs/product/message-risk-priority-v1.0.md) is the current scope. The first pilot uses Remi's confirmed accounts and a fixed London window (2026-09-09 18:00–2026-09-10 12:00). Owner save+enable at `/admin/ai` immediately queues analysis; results appear at `/dashboard/offhours-summary`. Disabled by default, no automatic daily schedule, real-time alerts, reply suggestions or automatic actions. This overrides conflicting requirements in the older PRD/TDD.
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
3. Message bodies are collected only after the revised OAuth agreement, encrypted at rest, and excluded from logs. Content views enforce live owner or explicit feature-scoped reviewer/CM access and are audited. Roster editing does not grant message access. AI never sends Lark messages; analysis uses owner-configured, evidence-bound features.

## Getting started

The backend baseline documents its local setup in [backend/README.md](backend/README.md). The frontend will be scaffolded in a separate pull request after the Phase 0 architecture decisions are recorded in `docs/adr/`.
