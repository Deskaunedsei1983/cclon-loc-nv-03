"""
OpenAI-kompatibler Wrapper. Waehlt die Agent-Variante per ENV:
  AGENT_IMPL=pydantic   (Default)  -> agent_pydantic.run_agent  (Tool-Calling, braucht pydantic-ai 1.x)
  AGENT_IMPL=langgraph             -> agent_langgraph.run_agent  (Critic-Loop)
Erscheint in Open WebUI als Modell "research-agent".
"""

import os
import time
import json
import uuid
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import common as C

IMPL = os.environ.get("AGENT_IMPL", "pydantic").lower()
if IMPL == "pydantic":
    try:
        from agent_pydantic import run_agent
    except Exception as _e:
        print(f"[agent] PydanticAI-Variante nicht ladbar ({_e}); Fallback auf LangGraph. "
              f"Fuer die pydantic-Variante 'pydantic-ai-slim[openai]' auf eine aktuelle "
              f"1.x-Version heben (die alte 0.0.20 kennt 'pydantic_ai.providers' noch nicht).",
              flush=True)
        from agent_langgraph import run_agent
else:
    from agent_langgraph import run_agent

app = FastAPI(title="research-agent (OpenAI-compatible)")
MODEL_ID = "research-agent"


@app.on_event("startup")
async def _start_blocklist_refresh():
    """OPT-IN: nur wenn BLOCKLIST_URL gesetzt ist -> stuendlicher Domain-Abgleich
    (sofort + alle BLOCKLIST_REFRESH_MIN Minuten) in den low-Tier."""
    if not getattr(C, "BLOCKLIST_URL", ""):
        return

    async def _loop():
        while True:
            await C.refresh_blocklist()
            await asyncio.sleep(max(5, C.BLOCKLIST_REFRESH_MIN) * 60)

    asyncio.create_task(_loop())


@app.get("/v1/models")
async def models():
    return {"object": "list",
            "data": [{"id": MODEL_ID, "object": "model",
                      "created": int(time.time()), "owned_by": f"local-{IMPL}"}]}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "impl": IMPL}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    stream = bool(body.get("stream", False))
    user_id = body.get("user") or "owui"

    answer = await run_agent(messages, user_id=user_id)
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if not stream:
        return JSONResponse({
            "id": cid, "object": "chat.completion", "created": created, "model": MODEL_ID,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": answer},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def sse():
        head = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
        yield f"data: {json.dumps(head)}\n\n"
        step = 400
        for i in range(0, len(answer), step):
            ch = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                  "choices": [{"index": 0, "delta": {"content": answer[i:i+step]}, "finish_reason": None}]}
            yield f"data: {json.dumps(ch)}\n\n"
        tail = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(tail)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")
