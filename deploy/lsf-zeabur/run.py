"""Exactly one OB lifespan and one internal provider; no production main."""
import asyncio
import importlib.metadata as metadata
import json
import os
import re
import signal
import sys
from collections import deque
from pathlib import Path
from environment import configure, RM_URL, RM_SHA

class TestSurface:
    """Dedicated MCP surface; no dashboard/config/import HTTP endpoints."""
    def __init__(self, app):
        self.app = app
        self.ready = False
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from starlette.responses import JSONResponse
            path = scope["path"]
            if path == "/health":
                response = JSONResponse({"status": "ok" if self.ready else "starting"}, status_code=200 if self.ready else 503)
                return await response(scope, receive, send)
            if path not in ("/mcp", "/mcp/"):
                return await JSONResponse({"error": "test_surface_only"}, status_code=404)(scope, receive, send)
        return await self.app(scope, receive, send)

def runtime_sources():
    import mcp
    import remember_me
    from remember_me_adapter import inspect_remember_me_contract, validate_remember_me_contract
    assert sys.version.split()[0] == "3.12.14"
    assert metadata.version("mcp") == "1.29.1"
    assert metadata.version("remember-me") == "0.1.0"
    dist = metadata.distribution("remember-me")
    direct = json.loads(dist.read_text("direct_url.json"))
    assert direct["url"] == RM_URL
    assert direct["archive_info"]["hashes"]["sha256"] == RM_SHA
    validate_remember_me_contract(inspect_remember_me_contract())
    return dict(python=sys.version.split()[0], executable=sys.executable,
                mcp=metadata.version("mcp"), mcp_source=mcp.__file__,
                remember_me=metadata.version("remember-me"), remember_me_source=remember_me.__file__,
                remember_me_release=RM_URL, remember_me_sha256=RM_SHA)

async def serve(token, root, port, ready=None, stop_event=None):
    configure(token, root, port)
    from observe import install_logging
    logs = install_logging([token])
    from initialize import lock_volume, initialize
    lock = lock_volume(root)
    handles = []
    seeded = False
    try:
        sources = runtime_sources()
        seeded = initialize(root)
        import server as ob
        from environment import SOURCE
        assert Path(ob.__file__).resolve() == SOURCE / "server.py"
        assert ob.config["buckets_dir"] == str(root / "buckets")
        assert ob._selected_asset_backend().name == "legacy"
        async def disabled_scheduler():
            return None
        ob.decay_engine.ensure_started = disabled_scheduler
        ob.decay_engine.start = disabled_scheduler
        from launcher import start, stop, build
        import provider_stub as stub
        handles.append(await start(stub.app, 18995))
        surface = TestSurface(build(ob, deque(maxlen=256)))
        handles.append(await start(surface, port, "0.0.0.0"))
        if seeded:
            from seed import readback
            await readback(ob)
        surface.ready = True
        event = stop_event or asyncio.Event()
        loop = asyncio.get_running_loop()
        installed = []
        if stop_event is None:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, event.set)
                installed.append(sig)
        waiter = asyncio.create_task(event.wait())
        try:
            if ready is not None:
                ready.set_result(dict(sources=sources, seeded=seeded, logs=logs))
            completed, _ = await asyncio.wait([waiter, *[h[1] for h in handles]], return_when=asyncio.FIRST_COMPLETED)
            if waiter not in completed:
                raise RuntimeError("test_service_unexpected_exit")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            for sig in installed:
                loop.remove_signal_handler(sig)
    finally:
        from launcher import stop
        errors = []
        for handle in reversed(handles):
            try:
                await stop(handle)
            except BaseException as exc:
                errors.append(type(exc).__name__)
        if "server" in sys.modules:
            ob = sys.modules["server"]
            if ob._LEGACY_POST_EFFECT_TASKS:
                await asyncio.gather(*list(ob._LEGACY_POST_EFFECT_TASKS))
        lock.close()
        if errors:
            raise RuntimeError("test_shutdown_failed")

def main():
    token = os.environ.get("OMBRE_MCP_QUERY_TOKEN", "")
    if os.environ.get("OB_LSF_TEST_SERVICE") != "true" or os.environ.get("OMBRE_MCP_ALLOW_QUERY_TOKEN") != "true":
        raise RuntimeError("test_opt_in_required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,}", token):
        raise RuntimeError("fresh_query_token_required")
    port = int(os.environ.get("PORT", "8080"))
    if not 1 <= port <= 65535 or port == 18995:
        raise RuntimeError("invalid_platform_port")
    root = Path("/data/lsf")
    if not root.is_dir() or not os.path.ismount(root):
        raise RuntimeError("dedicated_volume_mount_required")
    # Only the dedicated mount point ownership is initialized; no recursive changes.
    if os.getuid() == 0:
        os.chown(root, 10001, 10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    asyncio.run(serve(token, root, port))

if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never print exception payload, traceback, environment, URL or token.
        print("L-SF startup/runtime failed; inspect safe preflight evidence", file=sys.stderr)
        sys.exit(1)
