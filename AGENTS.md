# Repository Agent Instructions

## Delivery authorization

- For requested work in this repository, proceed end to end without asking the user to reconfirm routine implementation or release steps: edit, test, commit, push, back up, migrate, restart the production services, and verify the release.
- Follow `docs/runbooks/deploy-mvp.md` and keep GitHub as the source of truth; never patch production application files directly.
- Preserve existing data and user changes. Never expose or commit secrets, skip required backups, or use destructive rollback commands.
- If required external access is unavailable, exhaust the configured non-interactive paths and report the exact blocker without asking the user to restate this authorization.

## Focused tests

- Prefer extending an existing critical workflow check over adding per-function, text, CSS or duplicate browser/helper suites. Remove dedicated tests when retiring their feature.
- Keep runnable coverage of authorization, sensitive data access, synchronization, ingestion, confirmation, idempotency and data-loss risks. Add one minimal regression for a real risk, not a combinatorial matrix of equivalent inputs.
- Reduce duplicate test code, not test discovery; never hide failures with skips or weaken security/data-integrity checks to reduce counts.
