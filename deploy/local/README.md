# Local demo topology

A reproducible, disposable Docker Compose stack for local development: separate
OpenEMR, MariaDB, and AI-server services with persistent local state, fronted by a
local-only Caddy ingress (`emr.localhost`, `chat.localhost`) using Caddy's internal
development certificates. There is no public ingress, cloud provisioning, or
production deployment here — see [ARCHITECTURE.md](../../ARCHITECTURE.md) §9 for
the separate OCI deployment, which uses public sslip.io hostnames and Let's
Encrypt instead.

## Prerequisites

- Docker and Docker Compose.
- [uv](https://docs.astral.sh/uv/), for the AI-server image build and seeding.

## 1. Configure secrets

Secrets are never committed. From this directory:

```sh
cp .env.example .env
```

Fill in `.env`:

- `MARIADB_ROOT_PASSWORD`, `OPENEMR_MYSQL_PASSWORD`, `OPENEMR_ADMIN_PASSWORD`: any
  local-only values.
- `AI_SESSION_ENCRYPTION_KEY`: generate with
  `python -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`.
- `OPENEMR_OAUTH_*`: registered after OpenEMR is up (step 3 below). The AI server
  will fail to start without these; that is expected until you register a client.

No variable requires a paid plan. `GROQ_API_KEY` may stay blank — the AI server
starts and serves `/health` without it.

## 2. Start the stack

```sh
docker compose up -d --build
```

MariaDB and OpenEMR report healthy once OpenEMR finishes its first-run install
(a few minutes on a clean checkout). Follow progress with:

```sh
docker compose ps
docker compose logs -f openemr
```

## 3. Register the AI server as an OpenEMR OAuth client

Log in to OpenEMR at `https://emr.localhost` (routed through Caddy; your browser
will warn about the untrusted local development certificate the first time — this
is expected, see step 8) with `OE_USER`/`OE_PASS` from `.env`, then register a
confidential OAuth client (Admin > System > API Clients, or
`POST /oauth2/default/registration`) with redirect URI `OPENEMR_OAUTH_REDIRECT_URI`
and the scopes in `ai_server/app/auth.py` (`AuthSettings.scopes`) — `patient/*` only,
never `user/*` (TICK-033: a `user/*` scope makes OpenEMR show a genuine patient a
staff-style resource-permission consent screen at login, which this product must
never do). A confidential client registered with only `patient/*` scopes is enabled
automatically; no separate manual "Enable" step in Admin > System > API Clients is
needed for this client. Copy the issued client ID/secret into `.env`, then:

```sh
docker compose up -d ai-server
```

## 4. Enable the AI portal chat module

`docker compose up -d` already bind-mounts `openemr_modules/aeai-portal-chat` (TICK-012)
read-only into OpenEMR's custom-module directory, but OpenEMR only loads a custom module
once it has a `modules` row with `mod_active = 1`. Register and enable it the same way as
any other custom module: log in to OpenEMR at `https://emr.localhost` with `OE_USER`/
`OE_PASS`, then **Modules > Manage Modules > Custom Modules**, find "AEA Investors Portal
Chat", **Register**, then **Install**/**Enable**. A logged-in patient's portal home page
(`/portal/home.php`) then embeds the chat iframe; a logged-out visitor never sees it,
because the underlying `RenderEvent::EVENT_SECTION_RENDER_POST` hook only fires from that
authenticated page (see
[evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md](../../evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md)).

## 5. Seed synthetic data

```sh
../seed.sh
```

Writes offline synthetic identities to `generated-fixtures/local-demo/` (see
[TICK-006](../../tickets/TICK-006-build-synthetic-fixtures.md)). This data is
evaluation-only; it is not loaded into OpenEMR by this script.

## 6. Run health checks

```sh
curl -k https://chat.localhost/health
```

(`-k` skips curl's certificate trust check for Caddy's local development
certificate — see step 8.) Returns non-sensitive dependency reachability for the
AI server, OpenEMR API, OCR, and external LLM. `ocr` reports `ok` once the image's
pinned Tesseract is reachable. `openemr_api` reuses `OPENEMR_OAUTH_ISSUER`, which
now goes through Caddy's `emr.localhost` hostname (see `.env.example`), reachable
from both the browser and the AI-server container. `external_llm` is `unavailable`
whenever `GROQ_API_KEY` is blank, which is the default, paid-service-free path.

## 7. Verify restart persistence

```sh
docker compose restart ai-server
curl -k https://chat.localhost/health
```

The `ai-session-data` volume persists the SQLite WAL session store
(`AI_SESSION_DATABASE_PATH`) across the restart (NFR-31).

## 8. Verify the local Caddy ingress

Caddy is the only service with a published host port for HTTP/application
traffic; MariaDB has no host port at all, and the AI server's internal port
(8000) is reachable only from other containers, never directly from the host:

```sh
curl http://localhost:8000/health    # connection refused: not published to the host
```

HTTP redirects to HTTPS:

```sh
curl -kI http://emr.localhost/       # -> 3xx Location: https://emr.localhost/...
```

The `emr` hostname reaches OpenEMR, and the `chat` hostname proxies only to the AI
service:

```sh
curl -k https://emr.localhost/ | head -1     # OpenEMR's login page
curl -k https://chat.localhost/health        # the AI server's health endpoint
```

Local certificate state persists in the `caddy-data` volume across restarts (no
new certificate warning or delay after a restart):

```sh
docker compose restart caddy
curl -k https://emr.localhost/ | head -1
```

Because these are local development certificates from Caddy's internal CA (not a
publicly trusted one), curl needs `-k` and browsers show a one-time warning you
must accept; this is expected for a disposable local topology (see Caddyfile).

## Tear down

```sh
docker compose down        # keep volumes (state persists)
docker compose down -v     # also remove volumes (fresh next start)
```
