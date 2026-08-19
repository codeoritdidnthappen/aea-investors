# Local demo topology

A reproducible, disposable Docker Compose stack for local development: separate
OpenEMR, MariaDB, and AI-server services with persistent local state. There is no
public ingress, cloud provisioning, or production deployment here — see
[TICK-023](../../tickets/TICK-023-configure-oci-ingress.md) for local Caddy ingress
and [ARCHITECTURE.md](../../ARCHITECTURE.md) §9 for the separate OCI deployment.

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

Log in to OpenEMR at `https://localhost:${OPENEMR_HTTPS_PORT:-8443}` with
`OE_USER`/`OE_PASS` from `.env`, then register a confidential OAuth client (Admin >
System > API Clients, or `POST /oauth2/default/registration`) with redirect URI
`OPENEMR_OAUTH_REDIRECT_URI` and the scopes in `ai_server/app/auth.py`
(`AuthSettings.scopes`). Copy the issued client ID/secret into `.env`, then:

```sh
docker compose up -d ai-server
```

## 4. Seed synthetic data

```sh
../seed.sh
```

Writes offline synthetic identities to `generated-fixtures/local-demo/` (see
[TICK-006](../../tickets/TICK-006-build-synthetic-fixtures.md)). This data is
evaluation-only; it is not loaded into OpenEMR by this script.

## 5. Run health checks

```sh
curl http://localhost:${AI_SERVER_PORT:-8000}/health
```

Returns non-sensitive dependency reachability for the AI server, OpenEMR API, OCR,
and external LLM. `ocr` reports `ok` once the image's pinned Tesseract is reachable.
`openemr_api` reuses `OPENEMR_OAUTH_ISSUER`, which is intentionally the
browser-facing `localhost` address (see `.env.example`); the AI-server container
cannot resolve that address to the `openemr` container, so this check reports
`unavailable` in this topology until TICK-023's ingress gives both the browser and
the containers one shared hostname. `external_llm` is `unavailable` whenever
`GROQ_API_KEY` is blank, which is the default, paid-service-free path.

## 6. Verify restart persistence

```sh
docker compose restart ai-server
curl http://localhost:${AI_SERVER_PORT:-8000}/health
```

The `ai-session-data` volume persists the SQLite WAL session store
(`AI_SESSION_DATABASE_PATH`) across the restart (NFR-31).

## Tear down

```sh
docker compose down        # keep volumes (state persists)
docker compose down -v     # also remove volumes (fresh next start)
```
