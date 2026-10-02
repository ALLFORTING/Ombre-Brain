"""Deterministic loopback HTTP OpenAI-compatible provider; no direct business mocks."""
import hashlib
import json
from collections import Counter
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
COUNTS = Counter()
ROWS = []
def metadata(body):
    return dict(domain=["obweb-ls"], tags=["obweb-ls"], importance=5,
                valence=0.5, arousal=0.3, todos=[],
                suggested_name="synthetic-" + hashlib.sha256(body.encode()).hexdigest()[:8])
async def provider(request):
    data = await request.json()
    if request.url.path == "/v1/embeddings":
        inputs = data["input"]
        if isinstance(inputs, str):
            inputs = [inputs]
        COUNTS["embedding"] += 1
        ROWS.append(dict(kind="embedding", model=data["model"], input_count=len(inputs)))
        return JSONResponse(dict(object="list", model=data["model"],
            data=[dict(object="embedding", index=i, embedding=[1.0, 0.0, 0.0, 0.0])
                  for i, _ in enumerate(inputs)], usage=dict(prompt_tokens=1,total_tokens=1)))
    if request.url.path != "/v1/chat/completions":
        return JSONResponse({"error": {"type": "unsupported_path"}}, status_code=404)
    messages = data["messages"]
    system, body = messages[0]["content"], messages[-1]["content"]
    if "日记整理专家" in system:
        kind = "digest"
        if "蓝色纸卡" not in body or "绿色纸卡" not in body:
            return JSONResponse({"error": {"type": "unexpected_synthetic_input"}}, status_code=422)
        result = [dict(metadata(part), name=name, content=part)
                  for name,part in [
                    ("蓝色纸卡", "第一条隔离事项是准备蓝色纸卡。"),
                    ("绿色纸卡", "第二条独立事项是核对绿色纸卡。")]]
    elif "内容分析器" in system:
        kind, result = "analyze", metadata(body)
    elif "信息压缩专家" in system:
        kind = "dehydrate"
        result = dict(core_facts=[body], keywords=["obweb-ls"], summary=body[:50])
    else:
        return JSONResponse({"error": {"type": "unsupported_prompt"}}, status_code=422)
    COUNTS[kind] += 1
    ROWS.append(dict(kind=kind, model=data["model"], input_sha256=hashlib.sha256(body.encode()).hexdigest()))
    return JSONResponse(dict(id="synthetic-completion", object="chat.completion", created=0,
        model=data["model"], choices=[dict(index=0, message=dict(role="assistant",
        content=json.dumps(result, ensure_ascii=False)), finish_reason="stop")],
        usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2)))
app = Starlette(routes=[Route("/v1/embeddings", provider, methods=["POST"]),
                        Route("/v1/chat/completions", provider, methods=["POST"])])

