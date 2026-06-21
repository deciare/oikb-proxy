"""Integration test for oikb-proxy v2."""
import json
import sys
import urllib.request
import urllib.error


def fetch(url, method="GET", data=None):
    """Return (status_code, parsed_json_body)."""
    req = urllib.request.Request(url, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except Exception as e:
        return 0, {"error": str(e)}


def test():
    BASE = "http://127.0.0.1:18080"
    failures = 0

    def check(name, status, expected, body, checks=None):
        nonlocal failures
        if status != expected:
            print(f"  FAIL {name}: HTTP {status} (expected {expected})")
            failures += 1
            return False
        if checks:
            for key in checks:
                if key not in body:
                    print(f"  FAIL {name}: missing key '{key}'")
                    print(f"       body: {json.dumps(body)[:200]}")
                    failures += 1
                    return False
        print(f"  PASS {name}")
        return True

    def assert_eq(name, a, b):
        nonlocal failures
        if a != b:
            print(f"  FAIL {name}: {a!r} != {b!r}")
            failures += 1
        else:
            print(f"  PASS {name}")

    # ─── Test 1: OpenAPI spec ──────────────────────────────────────────
    print("\n─── OpenAPI / tool discovery ───")
    s, b = fetch(f"{BASE}/openapi.json")
    if check("GET /openapi.json", s, 200, b, ["openapi", "paths"]):
        assert_eq("  openapi version", b.get("openapi"), "3.0.2")
        paths = b.get("paths", {})
        assert_eq("  has /health", "/health" in paths, True)
        assert_eq("  has /history", "/history" in paths, True)
        assert_eq("  has /sync/{{identifier}}", "/sync/{identifier}" in paths, True)
        assert_eq(
            "  get_sync_status operationId",
            paths.get("/health", {}).get("get", {}).get("operationId"),
            "get_sync_status",
        )
        assert_eq(
            "  get_sync_history operationId",
            paths.get("/history", {}).get("get", {}).get("operationId"),
            "get_sync_history",
        )
        assert_eq(
            "  trigger_sync operationId",
            paths.get("/sync/{identifier}", {}).get("post", {}).get("operationId"),
            "trigger_sync",
        )

    # ─── Test 2: Health aggregation ────────────────────────────────────
    print("\n─── Health ───")
    s, b = fetch(f"{BASE}/health")
    if check("GET /health", s, 200, b, ["status", "sources"]):
        assert_eq("  overall status", b.get("status"), "ok")

    # ─── Test 3: Sync routing ──────────────────────────────────────────
    print("\n─── Sync routing ───")
    s, b = fetch(f"{BASE}/sync/airi-knowledge-base", method="POST")
    if check("POST /sync/airi-knowledge-base → alice", s, 200, b):
        assert_eq("  triggered", b.get("triggered"), True)
        assert_eq("  name", b.get("name"), "mock-alice-airi-knowledge-base")

    s, b = fetch(f"{BASE}/sync/bob-kb", method="POST")
    if check("POST /sync/bob-kb → bob", s, 200, b):
        assert_eq("  name", b.get("name"), "mock-bob-bob-kb")

    # KB UUID routing
    s, b = fetch(f"{BASE}/sync/abc123", method="POST")
    check("POST /sync/abc123 → alice", s, 200, b)

    s, b = fetch(f"{BASE}/sync/def456", method="POST")
    check("POST /sync/def456 → bob", s, 200, b)

    # Unknown identifier
    s, b = fetch(f"{BASE}/sync/nonexistent", method="POST")
    check("POST /sync/nonexistent → 404", s, 404, b)

    # ─── Test 4: Dry run passthrough ───────────────────────────────────
    print("\n─── Dry run ───")
    s, b = fetch(f"{BASE}/sync/airi-knowledge-base?dry_run=true", method="POST")
    if check("POST /sync/airi-knowledge-base?dry_run=true", s, 200, b):
        assert_eq("  dry_run", b.get("dry_run"), True)

    # ─── Test 5: History aggregation ───────────────────────────────────
    print("\n─── History ───")
    s, b = fetch(f"{BASE}/history")
    if check("GET /history", s, 200, b, ["entries"]):
        assert_eq("  has entries", len(b["entries"]) > 0, True)

    # ─── Test 6: Health/ready ──────────────────────────────────────────
    print("\n─── Health/ready ───")
    s, b = fetch(f"{BASE}/health/ready")
    check("GET /health/ready", s, 200, b, ["status"])

    # ─── Report ────────────────────────────────────────────────────────
    print()
    if failures:
        print(f"❌ {failures} FAILURE(S)")
        sys.exit(1)
    else:
        print("✅ All tests passed!")


if __name__ == "__main__":
    test()
