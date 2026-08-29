# Lark User Ingress

Bare-metal deployment bootstrap for the CM/AM Lark user-OAuth spike.

It intentionally provides only:

- `GET /healthz` for infrastructure verification;
- `GET /auth/lark/start` for OAuth authorization-code login;
- `GET /auth/lark/callback` for state validation, token exchange and encrypted server-side token storage.

It does not send Lark messages, and it does not read a group until a CM/AM has individually authorized their account and a group-binding flow is added.

## Required runtime configuration

`/etc/lark-user-ingress/app.env` is root-owned and read by the `larkapp` system account. It contains no Lark App Secret. The secret is kept in `/etc/lark-user-ingress/secrets/lark_app_secret` with mode `0640` and group `larkapp`.

Required variables:

```dotenv
NODE_ENV=production
PORT=3000
LARK_APP_ID=cli_aa047d00b5389e15
LARK_REDIRECT_URI=https://larkbot.ttapp.help/auth/lark/callback
LARK_APP_SECRET_FILE=/etc/lark-user-ingress/secrets/lark_app_secret
TOKEN_ENCRYPTION_KEY_B64=<32-random-byte-base64>
PGHOST=/var/run/postgresql
PGDATABASE=lark_ingress
PGUSER=larkapp
```
