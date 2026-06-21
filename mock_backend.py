"""Mock oikb servers for testing the v2 proxy.
Adds auth checking to simulate OIKB_API_KEY-protected endpoints."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn
import asyncio
import sys
import os

# Simulate OIKB_API_KEY auth on /sync and /history
AUTH_KEY = os.environ.get("MOCK_OIKB_API_KEY", "mock-backend-key")


def make_mock(name: str, auth_key: str | None = None):
    key = auth_key or AUTH_KEY

    async def check_auth(request: Request) -> bool:
        auth = request.headers.get("Authorization", "")
        return auth == f"Bearer {key}"

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "version": "0.3.5",
            "sources": [
                {"name": f"{name}-source", "status": "idle", "last_sync": None}
            ],
        })

    async def sync(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"detail": "Invalid API key"}, status_code=401)
        identifier = request.path_params.get("identifier", "")
        qp = request.url.query
        dry_run = "dry_run=true" in qp
        return JSONResponse({
            "triggered": not dry_run,
            "dry_run": dry_run,
            "name": f"{name}-{identifier}",
            "kb_id": identifier,
            "result": {"added": 0, "modified": 0, "deleted": 0} if dry_run else None,
        })

    async def history(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"detail": "Invalid API key"}, status_code=401)
        return JSONResponse({"entries": [
            {"source": f"{name}-source", "kb_id": "abc123", "status": "success"}
        ]})

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/health/ready", health, methods=["GET"]),
        Route("/history", history, methods=["GET"]),
        Route("/sync/{identifier:path}", sync, methods=["POST"]),
    ]
    return Starlette(routes=routes)


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18081
    name = sys.argv[2] if len(sys.argv) > 2 else f"mock-{port}"
    app = make_mock(name)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
