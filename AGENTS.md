# Repository Agent Instructions

## Delivery authorization

- For requested work in this repository, proceed end to end without asking the user to reconfirm routine implementation or release steps: edit, test, commit, push, back up, migrate, restart the production services, and verify the release.
- Follow `docs/runbooks/deploy-mvp.md` and keep GitHub as the source of truth; never patch production application files directly.
- Preserve existing data and user changes. Never expose or commit secrets, skip required backups, or use destructive rollback commands.
- If required external access is unavailable, exhaust the configured non-interactive paths and report the exact blocker without asking the user to restate this authorization.
