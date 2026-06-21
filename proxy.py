"""
oikb-proxy v2: routes /sync/{identifier} to per-user oikb daemons AND
exposes a synthetic /openapi.json so Open WebUI discovers the three tools.

How it works
────────────
1. Open WebUI fetches GET /openapi.json → proxy returns OpenAPI 3.0 spec
   with operation_ids matching oikb: get_sync_status, get_sync_history, trigger_sync
2. Open WebUI calls POST /sync/{identifier} → proxy looks up identifier
   in config.yaml → forwards to the right oikb backend with that backend's
   OIKB_API_KEY (NOT the proxy's key)
3. GET /health and GET /history aggregate across all backends

Auth
────
- Proxy auth: OIKB_PROXY_API_KEY env var → Bearer token Open WebUI sends
- Backend auth: each backend in config.yaml has an api_key field →
  proxy sends that key when calling /sync and /history on the backend
  (health endpoints on oikb are public, so no backend auth needed there)

Config schema (config.yaml)
────────────────────────────
  default_backend: null
  backend_timeout: 30
  routes:
    "kb-id-or-name":
      backend: "alice"
  backends:
    - name: "alice"
      url: "http://oikb-alice:8080"
      api_key: "oikb-daemon-key-alice"    # that container's OIKB_API_KEY
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.status import (
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_502_BAD_GATEWAY,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("oikb-proxy")


# ── config loading ────────────────────────────────────────────────────────


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("default_backend", None)
    cfg.setdefault("backend_timeout", 30)
    cfg.setdefault("routes", {})
    cfg.setdefault("backends", [])
    return cfg


def build_backend_index(backends: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for b in backends:
        name = b.get("name")
        if not name:
            log.warning("Backend entry missing 'name': %s", b)
            continue
        idx[name] = b
    return idx


def build_route_index(
    routes: dict, backends_idx: dict[str, dict]
) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for identifier, entry in routes.items():
        backend_name = entry.get("backend")
        if not backend_name:
            log.warning("Route '%s' missing 'backend', skipping", identifier)
            continue
        backend = backends_idx.get(backend_name)
        if not backend:
            log.warning(
                "Route '%s' references unknown backend '%s', skipping",
                identifier,
                backend_name,
            )
            continue
        idx[identifier] = backend
    return idx


# ── Synthetic OpenAPI spec ────────────────────────────────────────────────
# Open WebUI discovers tools by fetching /openapi.json.
# We must present the same operation_ids oikb uses.


def _build_openapi_spec(proxy_base_url: str = "") -> dict:
    return {
        "openapi": "3.0.2",
        "info": {
            "title": "oikb (proxied)",
            "description": (
                "Sync engine for Open WebUI Knowledge Bases. "
                "Trigger syncs, check status, and query history. "
                "Proxied to per-user oikb backends."
            ),
            "version": "0.3.5",
        },
        "servers": [{"url": proxy_base_url or "http://oikb-proxy:8080"}],
        "paths": {
            "/health": {
                "get": {
                    "operationId": "get_sync_status",
                    "summary": "Get sync status for all configured sources",
                    "description": (
                        "Returns the current sync status for every configured source, "
                        "including last sync time, duration, file counts, and any errors. "
                        "Use this to check if syncs are running and healthy."
                    ),
                    "responses": {
                        "200": {
                            "description": "Sync status",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"}
                                }
                            },
                        }
                    },
                }
            },
            "/history": {
                "get": {
                    "operationId": "get_sync_history",
                    "summary": "Query sync history log",
                    "description": (
                        "Returns recent sync history entries from the local database. "
                        "Filter by Knowledge Base ID or show only errors. "
                        "Each entry includes source, status, duration, and file change counts."
                    ),
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "default": 50},
                        },
                        {
                            "name": "kb_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "errors_only",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Sync history entries",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"}
                                }
                            },
                        }
                    },
                }
            },
            "/sync/{identifier}": {
                "post": {
                    "operationId": "trigger_sync",
                    "summary": "Trigger an immediate sync by alias or KB ID",
                    "description": (
                        "Triggers an immediate sync matching the given alias or "
                        "Knowledge Base ID. The sync runs asynchronously in the background. "
                        "Use get_sync_status to check progress. "
                        "Set dry_run=true to preview changes without uploading."
                    ),
                    "parameters": [
                        {
                            "name": "identifier",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "dry_run",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Sync triggered or dry-run result",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"}
                                }
                            },
                        },
                        "404": {
                            "description": "Unknown identifier",
                        },
                    },
                }
            },
        },
    }


# ── Proxy application ─────────────────────────────────────────────────────


class ProxyApp:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.cfg: dict = {}
        self.backends_idx: dict[str, dict] = {}
        self.route_idx: dict[str, dict] = {}
        self.proxy_api_key: str | None = os.environ.get("OIKB_PROXY_API_KEY")
        self.client: httpx.AsyncClient | None = None
        self._openapi_spec: dict = {}
        self._reload()

    def _reload(self):
        self.cfg = load_config(self.config_path)
        self.backends_idx = build_backend_index(self.cfg["backends"])
        self.route_idx = build_route_index(self.cfg["routes"], self.backends_idx)
        self._openapi_spec = _build_openapi_spec()
        log.info(
            "Loaded config: %d backends, %d routes",
            len(self.backends_idx),
            len(self.route_idx),
        )

    # ── Auth ──────────────────────────────────────────────────────────

    def _check_proxy_auth(self, request: Request) -> bool:
        if not self.proxy_api_key:
            return True
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {self.proxy_api_key}"
        return auth == expected

    def _backend_auth_headers(self, backend: dict) -> dict[str, str]:
        api_key = backend.get("api_key")
        if api_key:
            return {"Authorization": f"Bearer {api_key}"}
        return {}

    # ── Routing ───────────────────────────────────────────────────────

    def _resolve_backend(self, identifier: str) -> dict | None:
        if identifier in self.route_idx:
            return self.route_idx[identifier]
        default_name = self.cfg.get("default_backend")
        if default_name:
            return self.backends_idx.get(default_name)
        return None

    # ── OpenAPI endpoint ──────────────────────────────────────────────

    async def openapi(self, request: Request) -> JSONResponse:
        """GET /openapi.json — tool discovery. Public (matches oikb)."""
        return JSONResponse(self._openapi_spec)

    # ── Health ────────────────────────────────────────────────────────

    async def health(self, request: Request) -> JSONResponse:
        """GET /health — aggregate from all backends. Also /health/ready.

        Public — oikb leaves /health unauthenticated.

        Real oikb returns ``sources`` as a **dict** keyed by source path:
          {"sources": {"/data/kb": {"name": "kb", "status": "idle", ...}}}
        We merge dicts from all backends, prefixing keys to avoid collisions.
        """
        tasks = []
        for backend in self.cfg["backends"]:
            tasks.append(self._fetch_backend_health(backend))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        merged: dict[str, dict] = {}
        overall = "ok"

        for backend, result in zip(self.cfg["backends"], results):
            if isinstance(result, Exception):
                overall = "degraded"
                merged[f"{backend['name']}:error"] = {
                    "name": backend["name"],
                    "status": "error",
                    "error": str(result),
                }
            elif isinstance(result, dict):
                upstream_sources = result.get("sources", {})
                if isinstance(upstream_sources, dict):
                    for key, src in upstream_sources.items():
                        if isinstance(src, dict):
                            merged[f"{backend['name']}:{key}"] = src
                        else:
                            merged[f"{backend['name']}:{key}"] = {"value": src}
                elif isinstance(upstream_sources, list):
                    for src in upstream_sources:
                        if isinstance(src, dict):
                            name = src.get("name", backend["name"])
                            merged[f"{backend['name']}:{name}"] = src

        return JSONResponse(
            {"status": overall, "version": "0.3.5-proxy", "sources": merged}
        )

    async def _fetch_backend_health(self, backend: dict) -> dict:
        assert self.client is not None
        url = f"{backend['url'].rstrip('/')}/health"
        try:
            resp = await self.client.get(
                url, timeout=self.cfg["backend_timeout"]
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning("Health fetch failed for '%s': %s", backend["name"], exc)
            raise

    # ── Sync trigger ──────────────────────────────────────────────────

    async def sync_trigger(self, request: Request) -> Response:
        """POST /sync/{identifier} — route to correct backend."""
        if not self._check_proxy_auth(request):
            return JSONResponse(
                {"error": "unauthorized"}, status_code=HTTP_401_UNAUTHORIZED
            )

        identifier = request.path_params.get("identifier", "")
        if not identifier:
            return JSONResponse(
                {"error": "missing identifier"}, status_code=HTTP_404_NOT_FOUND
            )

        backend = self._resolve_backend(identifier)
        if not backend:
            return JSONResponse(
                {
                    "error": f"no backend found for identifier '{identifier}'",
                    "known_identifiers": list(self.route_idx.keys()),
                },
                status_code=HTTP_404_NOT_FOUND,
            )

        base = backend["url"].rstrip("/")
        upstream_url = f"{base}/sync/{identifier}"
        if request.url.query:
            upstream_url += f"?{request.url.query}"

        headers = self._backend_auth_headers(backend)
        headers["Content-Type"] = "application/json"

        body = await request.body()

        log.info(
            "Routing sync '%s' → %s (%s)",
            identifier,
            backend["name"],
            upstream_url,
        )

        assert self.client is not None
        try:
            upstream_resp = await self.client.request(
                method="POST",
                url=upstream_url,
                headers=headers,
                content=body,
                timeout=self.cfg["backend_timeout"],
            )
        except httpx.RequestError as exc:
            log.error("Backend request failed for '%s': %s", identifier, exc)
            return JSONResponse(
                {
                    "error": f"backend unreachable: {backend['name']}",
                    "detail": str(exc),
                },
                status_code=HTTP_502_BAD_GATEWAY,
            )

        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=dict(upstream_resp.headers),
        )

    # ── History ───────────────────────────────────────────────────────

    async def history(self, request: Request) -> Response:
        """GET /history — aggregate from all backends."""
        if not self._check_proxy_auth(request):
            return JSONResponse(
                {"error": "unauthorized"}, status_code=HTTP_401_UNAUTHORIZED
            )

        qp: dict[str, str] = {}
        for key in ("limit", "kb_id", "errors_only"):
            val = request.query_params.get(key)
            if val is not None:
                qp[key] = val

        tasks = []
        for backend in self.cfg["backends"]:
            tasks.append(self._fetch_backend_history(backend, qp))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        combined: list[dict] = []
        for backend, result in zip(self.cfg["backends"], results):
            if isinstance(result, Exception):
                log.warning(
                    "History fetch failed for '%s': %s", backend["name"], result
                )
                continue
            entries: list[dict] = []
            if isinstance(result, dict):
                entries = result.get("entries", [])
            elif isinstance(result, list):
                entries = result
            for entry in entries:
                entry["_backend"] = backend["name"]
            combined.extend(entries)

        return JSONResponse({"entries": combined})

    async def _fetch_backend_history(
        self, backend: dict, qp: dict[str, str]
    ):
        assert self.client is not None
        url = f"{backend['url'].rstrip('/')}/history"
        headers = self._backend_auth_headers(backend)
        try:
            resp = await self.client.get(
                url,
                headers=headers,
                params=qp if qp else None,
                timeout=self.cfg["backend_timeout"],
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.error("History fetch failed for '%s': %s", backend["name"], exc)
            raise

    # ── Reload ────────────────────────────────────────────────────────

    async def reload(self, request: Request) -> JSONResponse:
        """POST /reload — hot-reload config.yaml."""
        if not self._check_proxy_auth(request):
            return JSONResponse(
                {"error": "unauthorized"}, status_code=HTTP_401_UNAUTHORIZED
            )
        try:
            self._reload()
            return JSONResponse(
                {"status": "ok", "routes": len(self.route_idx)}
            )
        except Exception as exc:
            return JSONResponse(
                {"error": f"reload failed: {exc}"}, status_code=500
            )


# ── App factory ────────────────────────────────────────────────────────────


def make_app(config_path: str) -> Starlette:
    app_state = ProxyApp(config_path)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app_state.client = httpx.AsyncClient()
        try:
            yield
        finally:
            if app_state.client:
                await app_state.client.aclose()

    routes = [
        Route("/openapi.json", app_state.openapi, methods=["GET"]),
        Route("/health", app_state.health, methods=["GET"]),
        Route("/health/ready", app_state.health, methods=["GET"]),
        Route("/history", app_state.history, methods=["GET"]),
        Route("/sync/{identifier:path}", app_state.sync_trigger, methods=["POST"]),
        Route("/reload", app_state.reload, methods=["POST"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    return app


# ── CLI ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="oikb-proxy")
    parser.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml"
    )
    parser.add_argument("--port", type=int, default=8080, help="Listen port")
    parser.add_argument("--host", default="0.0.0.0", help="Listen host")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    app = make_app(str(config_path))

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
