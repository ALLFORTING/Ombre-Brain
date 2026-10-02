"""Bounded Linux deployment smoke, not the prior suite/failure matrix."""
import asyncio
import hashlib
import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parent

async def child():
    params = json.loads(sys.stdin.readline())
    from run import serve
    ready = asyncio.get_running_loop().create_future()
    event = asyncio.Event()
    task = asyncio.create_task(serve(params["token"], Path(params["root"]), params["port"], ready, event))
    try:
        done, _ = await asyncio.wait([ready, task], timeout=25, return_when=asyncio.FIRST_COMPLETED)
        if ready not in done:
            if task in done:
                await task
            raise RuntimeError("startup_timeout")
        info = ready.result()
        info["logs"].secrets.append(params["sentinel"])
        print(json.dumps({"ready": True, "seeded": info["seeded"], "sources": info["sources"]}), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        event.set()
        await task
    print(json.dumps({"stopped": True, "logs": info["logs"].projection()}), flush=True)

async def controller():
    import httpx
    evidence = ROOT / "evidence"
    evidence.mkdir(exist_ok=True)
    attempt = 1
    while (evidence / f"smoke-{attempt}.json").exists():
        attempt += 1
    data = ROOT / f".smoke-{attempt}"
    data.mkdir()
    token = secrets.token_urlsafe(32)
    sentinel = secrets.token_urlsafe(32)
    report = {"baseline": "885807cf460bec47af09812a523677d9dbb33eba", "S5": "INCOMPLETE", "cases": [], "image_build": "UNVERIFIED_DOCKER_UNAVAILABLE", "processes_closed": False}
    children = []
    def passed(name, **safe):
        report["cases"].append({"case": name, "result": "PASS", **safe})
    def hashes():
        return {str(p.relative_to(data)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (data / "buckets").rglob("*.md")}
    async def boot():
        proc = await asyncio.create_subprocess_exec(sys.executable, "-B", str(__file__), "--child", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        children.append(proc)
        proc.stdin.write((json.dumps({"token": token, "sentinel": sentinel, "root": str(data), "port": 18991}) + "\n").encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), 30)
        info = json.loads(line)
        if not info.get("ready"):
            report["child_startup_failure_type"] = info.get("failure_type")
            raise RuntimeError("child_startup_failed")
        return proc, info
    async def shutdown(proc):
        proc.stdin.write(b"stop\n")
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), 30)
        result = json.loads(line)
        await asyncio.wait_for(proc.wait(), 15)
        stderr = await proc.stderr.read()
        # Preserve booleans only; raw startup stderr is discarded.
        assert proc.returncode == 0 and result["stopped"]
        assert token.encode() not in stderr and sentinel.encode() not in stderr
        assert not result["logs"]["raw_value_leaked"]
        return result["logs"]
    def parse(response):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        result = next(json.loads(line[6:])["result"] for line in response.text.splitlines() if line.startswith("data: ") and "result" in json.loads(line[6:]))
        return result
    async def initialize(client):
        response = await client.post("/mcp", params={"token": token}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "zeabur-bounded-smoke", "version": "1"}}})
        result = parse(response)
        assert response.headers["mcp-session-id"]
        client.headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        client.headers["Mcp-Protocol-Version"] = result["protocolVersion"]
        response = await client.post("/mcp", params={"token": token}, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert response.status_code == 202
    async def rpc(client, method, params):
        return parse(await client.post("/mcp", params={"token": token}, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params}))
    async def call(client, name, args):
        result = await rpc(client, "tools/call", {"name": name, "arguments": args})
        assert not result.get("isError")
        return "\n".join(item["text"] for item in result["content"] if item["type"] == "text")
    try:
        proc, info = await boot()
        assert info["seeded"] is True
        report["runtime"] = info["sources"]
        passed("first_seed", count=len(hashes()))
        assert len(hashes()) == 2
        # Actual sockets prove bind addresses in this Linux network namespace.
        listener_rows = []
        for table in ("/proc/net/tcp",):
            for line in Path(table).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == "0A" and fields[1].endswith((":4A2F", ":4A33")):
                    listener_rows.append(fields[1])
        assert "00000000:4A2F" in listener_rows  # 18991
        assert "0100007F:4A33" in listener_rows  # 18995
        passed("socket_bindings", ob="0.0.0.0:18991", stub="127.0.0.1:18995")
        async with httpx.AsyncClient(base_url="http://127.0.0.1:18991", trust_env=False, timeout=20, headers={"Accept": "application/json, text/event-stream"}) as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/api/config")).status_code == 404
            assert (await client.post("/mcp", json={})).status_code == 401
            assert (await client.post("/mcp", params={"token": sentinel}, json={})).status_code == 401
            passed("health_surface_auth")
            await initialize(client)
            result = await rpc(client, "tools/list", {})
            assert len(result["tools"]) == 27
            trace = next(t for t in result["tools"] if t["name"] == "trace")["inputSchema"]["properties"]
            assert trace["append"]["type"] == "boolean" and "content" in trace
            passed("stateful_sse_schema", tool_count=27)
            text = await call(client, "breath", {"query": "OBWEB-LS-SEED", "mode": "full", "max_results": 5, "touch": False})
            assert "OBWEB-LS-SEED-PUBLIC-v1" in text and "c10000000002" not in text and "OBWEB-LS-HIDDEN" not in text
            await call(client, "hold", {"content": "ZEABUR-PREFLIGHT-HOLD-v1：隔离启动适配烟测。", "tags": "obweb-ls", "operation_id": "zeabur-preflight-hold-001"})
            passed("synthetic_read_and_one_write")
        before = hashes()
        assert len(before) == 3
        report["first_shutdown_logs"] = await shutdown(proc)
        proc2, info2 = await boot()
        assert info2["seeded"] is False and before == hashes()
        passed("same_root_restart_no_seed_no_overwrite", bucket_count=len(before))
        async with httpx.AsyncClient(base_url="http://127.0.0.1:18991", trust_env=False, timeout=20, headers={"Accept": "application/json, text/event-stream"}) as client:
            await initialize(client)
            text = await call(client, "breath", {"query": "ZEABUR-PREFLIGHT-HOLD", "mode": "summary", "touch": False})
            assert "ZEABUR-PREFLIGHT-HOLD" in text
            passed("restart_new_session_read")
        report["second_shutdown_logs"] = await shutdown(proc2)
        # No business replay or fault matrix. Just verify durable receipt placement.
        import sqlite3
        db = data / "buckets" / "bucket_history.sqlite3"
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            matches = []
            for table in tables:
                columns = [row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')]
                if "operation_id" in columns:
                    count = connection.execute('SELECT COUNT(*) FROM "' + table + '" WHERE operation_id=?', ("zeabur-preflight-hold-001",)).fetchone()[0]
                    if count:
                        matches.append({"table": table, "count": count})
            assert matches
            passed("receipt_in_test_volume", matches=matches)
        for port in (18991, 18995):
            with socket.socket() as sock:
                assert sock.connect_ex(("127.0.0.1", port)) != 0
        passed("listeners_closed")
        report["result"] = "PASS"
    except Exception as exc:
        report["result"] = "FAIL"
        report["failure_type"] = type(exc).__name__
    finally:
        for proc in children:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 20)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
        report["processes_closed"] = all(p.returncode is not None for p in children)
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        assert token not in serialized and sentinel not in serialized
        (evidence / f"smoke-{attempt}.json").write_text(serialized + "\n", encoding="utf-8")
        print(json.dumps({"result": report["result"], "cases": report["cases"], "processes_closed": report["processes_closed"], "failure_type": report.get("failure_type"), "evidence": f"smoke-{attempt}.json"}), flush=True)
    return 0 if report["result"] == "PASS" else 1

if __name__ == "__main__":
    try:
        if "--child" in sys.argv:
            asyncio.run(child())
        else:
            sys.exit(asyncio.run(controller()))
    except Exception as exc:
        print(json.dumps({"ready": False, "failure_type": type(exc).__name__}), flush=True)
        sys.exit(1)
