# Docker Compose Deployment Design

## Goal

Package the existing Forex Factory MVP backend so a server only needs Docker Compose, two secrets,
and one startup command. The deployment publishes only the FastAPI port `8000`; reverse proxy and
TLS remain outside this repository.

## Architecture

Docker Compose runs two services from one repository-owned image:

- `chrome` runs full Chromium under Xvfb with a persistent, non-default browser profile.
- `api` runs FastAPI, the collector, SQLite storage, and the asynchronous Kimi translator.

The API joins the Chrome service's network namespace and reaches CDP over
`http://127.0.0.1:9222`. This supports Chromium releases that restrict remote debugging to loopback
even when a wider bind address is requested. Port `9222` is never published. The stack publishes
`127.0.0.1:8000:8000` by default so only a local reverse proxy can reach the API.

## Persistence

- A named `database` volume mounts at `/app/data` and stores `forex_factory.sqlite3`.
- A named `chrome-profile` volume mounts at `/app/chrome-profile` and preserves cookies and browser
  state across container replacement.

Both volumes survive `docker compose down` and image upgrades. Operators must not use
`docker compose down -v` unless they intentionally want to delete stored data and browser state.

## Image and Runtime

The image uses Python 3.12 on Debian and installs Chromium, Xvfb, xauth, fonts, and curl. It installs the
locked Python production dependencies with `uv`, copies the application, and runs as an unprivileged
application user. The same immutable image is shared by both services; Compose supplies a different
command to each container.

The existing Chrome launcher gains environment-controlled bind address and an explicit container
mode for `--no-sandbox` and `--disable-dev-shm-usage`. Compose starts Xvfb with a readiness probe,
uses a private writable X11 socket directory, and removes only stale locks belonging to its dedicated
profile. Local execution remains secure by default: CDP binds to `127.0.0.1` and sandbox disabling is
opt-in.

## Configuration and Secrets

The operator copies `.env.example` to `.env` and changes only:

- `APP_API_KEY`
- `MOONSHOT_API_KEY`

`APP_PORT` defaults to `8000`. Compose overrides container-specific paths and CDP URL, so the same
application settings continue to support non-Docker local development. `.env` remains ignored and is
never copied into the image because `.dockerignore` excludes it.

The default translation configuration targets a Kimi Code membership key at
`https://api.kimi.com/coding/v1` with model `k3-256k`; both values remain configurable.

## Startup, Health, and Recovery

The `chrome` health check queries `/json/version`. The `api` service waits for a healthy browser and
then starts Uvicorn. The API health check queries `/health`. Both services use `restart:
unless-stopped`.

Collector and Kimi failures retain the existing behavior: source collection and translation run in
separate background loops, and transient failures are retried on later cycles without preventing the
HTTP API from serving stored data.

## Operator Workflow

Initial deployment:

```bash
cp .env.example .env
# edit APP_API_KEY and MOONSHOT_API_KEY
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

Upgrade:

```bash
git pull
docker compose up -d --build
```

Diagnostics use `docker compose ps` and `docker compose logs -f`.

## Verification

- Existing Python tests and Ruff checks remain green.
- Chrome launcher tests execute a fake browser binary and assert observable arguments.
- `docker compose config` validates the resolved Compose model.
- The image builds successfully.
- The full stack starts, both health checks pass, and host port `8000` answers `/health`.
- The resolved Compose model publishes no CDP port and mounts both persistent volumes.

## Non-Goals

- Nginx, domain, TLS certificates, and firewall configuration.
- PostgreSQL, Redis, Kubernetes, or a container registry.
- APNs push notifications.
- Automated deletion or migration of existing local SQLite/browser data.
