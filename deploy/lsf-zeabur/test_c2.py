"""Bounded WSL C2 checks; no Docker/cloud, production data or full suite."""
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent

def hashes(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*.md")}

def snapshot(root):
    db = root/"buckets/bucket_history.sqlite3"
    with sqlite3.connect(db) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'")]
        rows = {t:conn.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables}
    with sqlite3.connect(root/"buckets/embeddings.db") as conn:
        rows["embeddings"] = conn.execute("SELECT * FROM embeddings").fetchall()
    return dict(files=hashes(root),rows=rows,seed=(root/".synthetic-initialized.json").read_text())

async def worker(params):
    if params["action"]=="serve":
        from run import serve
        import httpx
        token="synthetic-local-test-token-with-no-external-permission"
        root=Path(params["root"])
        ready=asyncio.get_running_loop().create_future()
        event=asyncio.Event()
        task=asyncio.create_task(serve(token,root,18993,ready,event))
        try:
            done,_=await asyncio.wait([task,ready],timeout=30,return_when=asyncio.FIRST_COMPLETED)
            if ready not in done:
                if task in done:
                    await task
                raise RuntimeError("test_service_start_timeout")
            info=ready.result()
            async with httpx.AsyncClient(base_url="http://127.0.0.1:18993",
                headers={"Accept":"application/json, text/event-stream"},timeout=15) as client:
                response=await client.post("/mcp",params={"token":token},json={
                    "jsonrpc":"2.0","id":1,"method":"initialize","params":{
                        "protocolVersion":"2025-03-26","capabilities":{},
                        "clientInfo":{"name":"C2-local-validation","version":"1"}}})
                assert response.status_code==200
                session=response.headers["mcp-session-id"]
                client.headers.update({"Mcp-Session-Id":session,"Mcp-Protocol-Version":"2025-03-26"})
                response=await client.post("/mcp",params={"token":token},json={
                    "jsonrpc":"2.0","method":"notifications/initialized"})
                assert response.status_code==202
                response=await client.post("/mcp",params={"token":token},json={
                    "jsonrpc":"2.0","id":2,"method":"tools/call","params":{
                        "name":"breath","arguments":{"query":"C2HISTORY","domain":"c2-history",
                            "as_of":"2026-10-01T18:00:00+08:00","touch":False}}})
                assert response.status_code==200 and "OLD-V1" in response.text
            return dict(c2=info["c2"],real_mcp=True,snapshot=snapshot(root),
                        runtime=info["sources"])
        finally:
            event.set()
            await task
    from environment import configure
    root = Path(params["root"])
    configure("synthetic-local-test-token-with-no-external-permission",root,18993)
    from run import runtime_sources
    runtime_sources()
    from initialize import lock_volume, initialize
    lock = lock_volume(root)
    initialize(root)
    import server as ob
    from launcher import start, stop
    import provider_stub as stub
    provider = stub.app
    if params["action"] == "fail_index":
        from starlette.responses import JSONResponse
        class RefuseEmbedding:
            async def __call__(self,scope,receive,send):
                if scope.get("path")=="/v1/embeddings":
                    return await JSONResponse({"error":{"type":"synthetic_failure"}},status_code=400)(scope,receive,send)
                return await stub.app(scope,receive,send)
        provider = RefuseEmbedding()
    handle = await start(provider,18995)
    try:
        if params["action"]=="prepare":
            result = await ob.hold("obweb-ls-preserved-write",operation_id="obweb-ls-c2-preserve")
            assert "新建" in result
            old = [b for b in await ob.bucket_mgr.list_all() if b["content"]=="obweb-ls-preserved-write"][0]
            ob.bucket_mgr.record_letter("obweb-ls-preserved-letter",old["id"])
            return dict(snapshot=snapshot(root),old_id=old["id"])
        from c2_seed import initialize_c2, IDS
        result = await initialize_c2(ob,root,lock)
        state = json.loads((root/".c2-v1.json").read_text())
        assert len(state["buckets"])==21 and len(state["vectors"])==20
        assert not await ob.embedding_engine.get_embedding(IDS[7])
        if params["action"]=="read":
            from c2_vectors import valid_unit
            before = snapshot(root)
            cache_path=Path(ob.dehydrator.cache_db_path)
            cache_before=hashlib.sha256(cache_path.read_bytes()).hexdigest()
            long_bucket = await ob.bucket_mgr.get(IDS[20])
            summary = await ob.breath(query="C2-PAGE-LONG",domain="c2-page",mode="summary",touch=False,
                                      max_tokens=10000,max_results=50)
            assert "C2 compact summary" in summary and "C2-LONG-TAIL" not in summary
            assert any(r["kind"]=="dehydrate" for r in stub.ROWS)
            rendered = await ob.breath(query="C2-PAGE-LONG",domain="c2-page",mode="full",touch=False,
                                       max_tokens=20000,max_results=50)
            assert "C2-LONG-HEAD" in rendered and "C2-LONG-TAIL" in rendered
            tiny = await ob.breath(query="C2-PAGE-LONG",domain="c2-page",mode="full",touch=False,max_tokens=20)
            assert "已截断" in tiny
            pages, cursor = [], ""
            for _ in range(12):
                page = await ob.breath(query="C2PAGE",domain="c2-page",mode="summary",touch=False,
                                      max_results=2,max_tokens=180,cursor=cursor)
                pages.extend(re.findall(r"\[bucket_id:(c2\d+)\]",page))
                cursor_match = re.search(r"下一页 cursor: (\S+)",page)
                cursor = cursor_match[1] if cursor_match else ""
                if not cursor:
                    break
            assert not cursor and set(pages)==set(IDS[15:]) and len(pages)==6, pages
            old = await ob.breath(query="C2HISTORY",domain="c2-history",as_of="2026-10-01T18:00:00+08:00",touch=False)
            new = await ob.breath(query="C2HISTORY",domain="c2-history",as_of="2026-10-02T12:00:00+08:00",touch=False)
            assert "OLD-V1" in old and "NEW-V2" not in old and "NEW-V2" in new
            equal = await ob.breath(query="C2HISTORY",domain="c2-history",
                as_of="2026-10-02T00:00:00+08:00",touch=False)
            assert "NEW-V2" in equal and "OLD-V1" not in equal
            tags = await ob.breath(domain="c2-lex",tags_filter=["C2-TAG-EXACT"],touch=False)
            assert set(re.findall(r"\[bucket_id:(c2\d+)\]",tags))=={IDS[2],IDS[4]}
            today = await ob.breath(domain="c2-lex",tags_filter=["C2-TAG-EXACT"],touch=False,
                importance_min=7,date_from=state["today"],date_to=state["today"])
            assert set(re.findall(r"\[bucket_id:(c2\d+)\]",today))=={IDS[2]}
            zero = await ob.breath(domain="c2-lex",tags_filter=["C2-ZERO"],resonance="0,0",touch=False)
            assert zero.index(IDS[2]) < zero.index(IDS[4])
            for arguments,bid in [
                ({"domain":"session","topic_filter":["C2-TOPIC"]},IDS[12]),
                ({"feels":True,"tags_filter":["C2-FEEL"]},IDS[11]),
                ({"domain":"c2-lex","importance_min":7},IDS[4]),
                ({"domain":"c2-emerge"},IDS[13]),
            ]:
                assert bid in await ob.breath(**arguments,touch=False)
            empty = await ob.breath(query="C2PAGE",domain="c2-missing",touch=False)
            assert not re.findall(r"\[bucket_id:(c2\d+)\]",empty)
            hidden = await ob.breath(query="C2STATE",domain="c2-state",touch=False,max_results=50)
            assert IDS[7] not in hidden and "C2-SEALED" not in hidden and IDS[6] not in hidden
            visible = await ob.breath(query="C2STATE",domain="c2-state",touch=False,
                include_dormant=True,include_sealed=True,max_results=50)
            assert all(bid in visible for bid in IDS[6:11])
            for arguments,selector in [
                ({"query":"C2PAGE","arousal":0},"ordinary_query"),
                ({"domain":"session","mode":"full"},"session"),
                ({"feels":True,"include_dormant":True},"feel"),
                ({"resonance":"0,0","mode":"full"},"resonance"),
                ({"tags_filter":["c2-v1"],"mode":"full"},"tags_only"),
                ({"importance_min":7,"min_score":0},"importance_only"),
                ({"min_score":0},"default_emergence"),
                ({"query":"C2HISTORY","as_of":"2026-10-01","importance_min":7},"historical_query"),
                ({"mailbox":True},"mailbox"),
            ]:
                error=await ob.breath(**arguments,touch=False)
                assert f"breath mode={selector}" in error and "不支持参数" in error
            public = await ob.breath(mailbox=True,mailbox_limit=2)
            both = await ob.breath(mailbox=True,mailbox_limit=2,include_sealed=True)
            assert "PUBLIC" in public and "HIDDEN" not in public and "HIDDEN" in both
            values = [await ob.embedding_engine.get_embedding(IDS[n]) for n in (2,4,5)]
            assert all(valid_unit(v) for v in values) and len({tuple(v) for v in values})==3
            hits = await ob.embedding_engine.search_similar("C2-TAG-EXACT",top_k=30,candidate_ids={IDS[2],IDS[4],IDS[5]})
            scores = dict(hits)
            assert scores[IDS[2]] > scores[IDS[4]] > scores[IDS[5]]
            # Non-C2 input still traverses the real provider path unchanged.
            assert await ob.embedding_engine.embed_text("obweb-ls-legacy-query")==[1,0,0,0]
            legacy = await ob.dehydrator.dehydrate("obweb-ls legacy "*150,cache_read=False,cache_write=False)
            assert "C2 compact summary" not in legacy
            assert snapshot(root)==before
            assert hashlib.sha256(cache_path.read_bytes()).hexdigest()==cache_before
            result.update(real_summary=True,real_vector=True,pages=pages,
                          history_boundaries=True,nine_selector_paths=True,readonly_snapshot_preserved=True,
                          stored_long_chars=len(long_bucket["content"]))
        result.update(snapshot=snapshot(root),provider_counts=dict(stub.COUNTS))
        return result
    finally:
        await stop(handle)
        lock.close()

def child(root, action, ok=True):
    proc = subprocess.run([sys.executable,"-B",str(__file__),"--worker"],
        input=json.dumps(dict(root=str(root),action=action)),text=True,capture_output=True,timeout=60)
    result = json.loads(proc.stdout)
    assert bool(proc.returncode==0)==ok, result
    return result

def test_incremental_preservation_restart_real_paths(tmp_path):
    root = tmp_path/"lsf"
    prior = child(root,"prepare")
    first = child(root,"read")
    second = child(root,"read")
    assert first["initialized"] and not second["initialized"]
    assert first["snapshot"]==second["snapshot"]
    assert first["today"]==second["today"]
    for path,h in prior["snapshot"]["files"].items():
        assert first["snapshot"]["files"][path]==h
    assert prior["snapshot"]["seed"]==first["snapshot"]["seed"]
    for table,rows in prior["snapshot"]["rows"].items():
        assert all(row in first["snapshot"]["rows"][table] for row in rows)
    assert first["provider_counts"]["embedding"] > second["provider_counts"]["embedding"]

@pytest.mark.parametrize("failure",["partial","id","directory","identity","index","fixture","incomplete_manifest"])
def test_fail_closed(tmp_path,failure):
    root = tmp_path/"lsf"
    child(root,"prepare")
    marker = root/".c2-v1.json"
    if failure=="index":
        result = child(root,"fail_index",False)
        assert result["error"]=="c2_index_failed"
        assert json.loads(marker.read_text())["status"]=="started"
    elif failure in ("identity","fixture","incomplete_manifest"):
        child(root,"read")
        if failure=="identity":
            state=json.loads(marker.read_text()); state["baseline"]="wrong"
            marker.write_text(json.dumps(state))
        elif failure=="fixture":
            path=root/"buckets"/json.loads(marker.read_text())["buckets"][0]["path"]
            path.write_text(path.read_text()+"changed")
        else:
            state=json.loads(marker.read_text()); state["vectors"]=[]
            marker.write_text(json.dumps(state))
    elif failure=="partial":
        # Real interrupted initializer marker, identity intact.
        child(root,"fail_index",False)
    elif failure=="directory":
        (root/"buckets/dynamic/c2-v1").mkdir()
    else:
        import frontmatter
        path=root/"buckets/dynamic/foreign.md"
        path.write_text(frontmatter.dumps(frontmatter.Post("foreign",id="c20000000001")))
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    refused=child(root,"initialize",False)
    expected={"partial":"c2_partial_requires_review","index":"c2_partial_requires_review",
              "identity":"c2_identity_conflict","fixture":"c2_fixture_changed",
              "directory":"c2_directory_collision","id":"c2_id_collision",
              "incomplete_manifest":"c2_incomplete_manifest"}
    assert refused["error"]==expected[failure]
    after={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    assert after==before

def test_run_uses_one_lock_before_public_socket():
    import ast
    tree=ast.parse((HERE/"run.py").read_text())
    serve=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="serve")
    calls=[n for n in ast.walk(serve) if isinstance(n,ast.Call)]
    assert len([n for n in calls if isinstance(n.func,ast.Name) and n.func.id=="lock_volume"])==1
    c2=next(n for n in calls if isinstance(n.func,ast.Name) and n.func.id=="initialize_c2")
    public=next(n for n in calls if isinstance(n.func,ast.Name) and n.func.id=="start" and len(n.args)==3)
    assert c2.lineno < public.lineno

def test_real_service_initializes_before_requests_and_restarts(tmp_path):
    root=tmp_path/"lsf"
    prior=child(root,"prepare")
    first=child(root,"serve")
    second=child(root,"serve")
    assert first["c2"]["initialized"] and not second["c2"]["initialized"]
    assert first["snapshot"]==second["snapshot"]
    assert all(first["snapshot"]["files"][p]==h for p,h in prior["snapshot"]["files"].items())

if __name__=="__main__" and "--worker" in sys.argv:
    # Business imports/logs remain off stdout; only safe synthetic result projection.
    import contextlib
    params=json.loads(sys.stdin.read())
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result=asyncio.run(worker(params))
        print(json.dumps(result))
    except Exception as exc:
        import traceback
        frames=traceback.extract_tb(exc.__traceback__)
        print(json.dumps({"error":str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__,
                          "location":f"{Path(frames[-1].filename).name}:{frames[-1].lineno}"}))
        sys.exit(1)
