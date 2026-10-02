"""Reusable launcher builder; avoids production main entrypoint scheduler."""
import asyncio
import socket
import uvicorn
from observe import Observe
def build(ob, rows):
    app = ob.build_streamable_http_app()
    ob.add_mcp_auth_middleware(app)
    ob.add_http_cors_middleware(app)
    ob.add_mcp_diagnostic_middleware(app)
    ob.install_uvicorn_access_log_redaction()
    assert ob.mcp.settings.stateless_http is False
    assert ob.mcp.settings.json_response is False
    assert not ob.OMBRE_HOOK_URL and ob.OMBRE_HOOK_SKIP
    return Observe(app, rows)
async def start(app, port, host="127.0.0.1"):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    runner = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=True,
                        log_level="info", lifespan="on", timeout_graceful_shutdown=10))
    # The enclosing controller owns signal handling and cleanup.
    runner.capture_signals = __import__("contextlib").nullcontext
    task = asyncio.create_task(runner.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not runner.started:
                if task.done():
                    await task
                    raise RuntimeError("startup_failed")
                await asyncio.sleep(0.02)
    except BaseException:
        runner.should_exit = True
        await asyncio.gather(task, return_exceptions=True)
        sock.close()
        raise
    return runner, task, sock
async def stop(handle):
    runner, task, sock = handle
    runner.should_exit = True
    try:
        await asyncio.wait_for(task, 15)
    finally:
        sock.close()


# Negative auth configuration probes never enter SDK; exactly one real MCP lifespan.
def auth_probe(ob, rows):
    from starlette.applications import Starlette
    app = Starlette()
    ob.add_mcp_auth_middleware(app)
    ob.install_uvicorn_access_log_redaction()
    return Observe(app, rows)

