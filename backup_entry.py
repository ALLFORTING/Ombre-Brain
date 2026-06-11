"""Render entry point that adds the authenticated backup export endpoint."""

import asyncio
import logging
import threading

import httpx

import server
from backup_export import backup_payload_json, verify_github_oidc


logger = logging.getLogger("ombre_brain.backup")


@server.mcp.custom_route("/api/backup/export", methods=["GET"])
async def backup_export_endpoint(request):
    from starlette.responses import JSONResponse, Response

    authorization = request.headers.get("authorization", "")
    token = (
        authorization[7:].strip()
        if authorization.lower().startswith("bearer ")
        else ""
    )
    try:
        claims = await verify_github_oidc(token)
    except Exception as exc:
        logger.warning("Rejected backup export request: %s", exc)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        payload = await asyncio.to_thread(
            backup_payload_json,
            server.config["buckets_dir"],
        )
    except Exception as exc:
        logger.exception("Backup export failed")
        return JSONResponse({"error": str(exc)}, status_code=500)

    logger.info(
        "Backup export completed for %s run %s",
        claims.get("repository"),
        claims.get("run_id"),
    )
    return Response(
        payload,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def run() -> None:
    transport = server.config.get("transport", "stdio")
    logger.info("Ombre Brain starting with backup export | transport: %s", transport)

    if transport not in ("sse", "streamable-http"):
        server.mcp.run(transport=transport)
        return

    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    async def keepalive_loop():
        await asyncio.sleep(10)
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await client.get(
                        f"http://localhost:{server.OMBRE_PORT}/health",
                        timeout=5,
                    )
                    logger.debug("Keepalive ping OK")
                except Exception as exc:
                    logger.warning("Keepalive ping failed: %s", exc)
                await asyncio.sleep(60)

    def start_keepalive():
        loop = asyncio.new_event_loop()
        loop.run_until_complete(keepalive_loop())

    threading.Thread(target=start_keepalive, daemon=True).start()

    if transport == "streamable-http":
        app = server.mcp.streamable_http_app()
    else:
        app = server.mcp.sse_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    uvicorn.run(app, host="0.0.0.0", port=server.OMBRE_PORT)


if __name__ == "__main__":
    run()
