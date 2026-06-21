# oikb-proxy

A routing proxy for [oikb](https://github.com/open-webui/oikb) that enables multi-user knowledge base sync through a single Open WebUI Tool Server connection.

## The Problem

oikb syncs Open WebUI knowledge bases to vector databases. That works well until the system needs to handle knowledge bases belonging to different users.

Each oikb daemon authenticates with a single `OPEN_WEBUI_API_KEY`, which is scoped to one user's knowledge bases. If you have multiple users (especially in deployments with `BYPASS_ADMIN_ACCESS_CONTROL=False`), you need *one oikb daemon per user* — but Open WebUI's Tool Server integration only accepts a single URL.

**You can't point Open WebUI at multiple oikb instances. You need something that looks like one oikb but knows which backend to call for each knowledge base.**

## The Solution

oikb-proxy sits between Open WebUI and your per-user oikb daemons. Open WebUI sees a single endpoint. The proxy routes each `/sync/{identifier}` call to the correct backend based on a straightforward config file.

- **Looks like one oikb** — serves the same `/openapi.json`, `/health`, `/history`, `/sync/{identifier}` endpoints
- **Routes intelligently** — maps KB IDs and friendly names to backends via `config.yaml`
- **Aggregates** — `/health` and `/history` merge responses from all backends
- **Two-layer auth** — Open WebUI authenticates to the proxy; the proxy authenticates separately to each backend

## Auth Architecture

| Layer | Credential | Where configured |
|---|---|---|
| Open WebUI → proxy | `OIKB_PROXY_API_KEY` (Bearer token) | Env var on proxy container |
| Proxy → oikb backend | `api_key` per backend | `config.yaml` |
| oikb → Open WebUI API | `OPEN_WEBUI_API_KEY` | Env var on each oikb container |

**The proxy never forwards the incoming Authorization header.** It validates it against `OIKB_PROXY_API_KEY`, then substitutes the backend's own key when calling `/sync` and `/history` on upstream oikb instances. `/health` calls are unauthenticated (oikb leaves health endpoints public).

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /openapi.json` | Public | Tool discovery — exposes `get_sync_status`, `get_sync_history`, `trigger_sync` |
| `GET /health` | Public | Aggregated health across all backends |
| `GET /health/ready` | Public | Liveness probe |
| `POST /sync/{identifier}` | Proxy key | Route sync to the correct backend |
| `GET /history` | Proxy key | Aggregated sync history across all backends |
| `POST /reload` | Proxy key | Hot-reload `config.yaml` without restarting |

## Quickstart

### 1. Start per-user oikb daemons

Each user needs their own oikb container with:

```yaml
# docker-compose.yml snippet
services:
  oikb-alice:
    image: ghcr.io/open-webui/oikb:latest
    environment:
      - OPEN_WEBUI_URL=http://open-webui:8080
      - OPEN_WEBUI_API_KEY=${ALICE_OWUI_API_KEY}
      - OIKB_API_KEY=${OIKB_ALICE_API_KEY}
    volumes:
      - "./volumes/oikb/alice.yaml:/data/.oikb.yaml:ro"
    command: daemon
```

### 2. Build and run the proxy

```yaml
services:
  oikb-proxy:
    build: ./oikb-proxy
    environment:
      - OIKB_PROXY_API_KEY=${OIKB_PROXY_API_KEY}
    volumes:
      - "./volumes/oikb-proxy/config.yaml:/data/config.yaml:ro"
    ports:
      - "18080:8080"
```

### 3. Configure routing

```yaml
# config.yaml
default_backend: null
backend_timeout: 30

backends:
  - name: alice
    url: http://oikb-alice:8080
    api_key: oikb-daemon-key-alice
  - name: bob
    url: http://oikb-bob:8080
    api_key: oikb-daemon-key-bob

routes:
  # Map by friendly name (what the agent calls trigger_sync with)
  "alice-kb":
    backend: alice
  "bob-kb":
    backend: bob

  # Map by KB UUID (from oikb.yaml)
  "08ebc7ee-bc1e-498e-9acb-92aac2ffb499":
    backend: alice
```

### 4. Point Open WebUI at the proxy

In Open WebUI Admin Settings → Tools, set the Tool Server URL to `http://oikb-proxy:8080` (or `http://your-host:18080` if published with a port).

**After changing the URL, re-assign your models to the new tool server.** The tool ID changes when the server URL changes, and existing model assignments don't automatically update.

## Testing

The repo includes mock backends for integration testing:

```bash
# Terminal 1: start two mock backends
python mock_backend.py 18081 mock-alice &
python mock_backend.py 18082 mock-bob &

# Terminal 2: start proxy with test config
python proxy.py --config config.test.yaml --port 18080

# Terminal 3: run the test suite
python test_proxy.py
```

The test suite (`test_proxy.py`) validates:
- OpenAPI spec discovery (`/openapi.json`)
- Health aggregation (`/health`, `/health/ready`)
- Sync routing by both friendly name and KB UUID
- Unknown identifier → 404 handling
- Dry run passthrough (`?dry_run=true`)
- History aggregation (`/history`)

## Adding a New User

1. Create a new oikb container with that user's `OPEN_WEBUI_API_KEY`
2. Add a `backend` entry in `config.yaml` with the container's `OIKB_API_KEY`
3. Add `route` entries mapping the KB's friendly name and UUID to the new backend
4. `POST /reload` (or restart the proxy) — no container rebuild needed

## Project Structure

```
oikb-proxy/
├── proxy.py                   # Starlette application (~540 lines)
├── config.example.yaml        # Annotated production config
├── config.test.yaml           # Config for integration tests
├── Dockerfile                 # Container build (python:3.12-slim)
├── docker-compose.addendum.yml# Drop-in compose snippet
├── mock_backend.py            # Mock oikb servers for testing
└── test_proxy.py              # Test suite
```
