# ======================================================================
# 助手问答接口（ChatGPT 式流式对话的 backend）
#
# 端点：POST /api/assistant/chat
#   - 收 {message}；先跑「多智能体管线」（查询改写→主管分派→检索员+图谱员→rerank），
#     把中间的轨迹打包成 SSE 的 trace 事件发给前端（用于「思考过程」面板）；
#   - 再用 LLM 基于检索到的资料，逐 token 流式生成回答（SSE 的 token 事件）。
#
# 为什么用 SSE（Server-Sent Events）：它天然是「服务器单向推流」，正好适配
# 打字机式的流式回答；前端用 fetch + ReadableStream 解析即可，比 WebSocket 简单。
# ======================================================================
import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.limiter import limiter
from app.rag import api as ai_api   # 复用 _get_store / ensure_index / get_embeddings
from app.rag.agent import run_assistant_pipeline
from app.rag.llm import get_llm

router = APIRouter()


class AssistantRequest(BaseModel):
    """/api/assistant/chat 请求体：访客的提问。history 预留给以后做多轮记忆。"""
    message: str
    history: list = []


def _sse(event: str, data) -> str:
    """把数据包装成一条 SSE 消息：'event: 名字\\ndata: JSON\\n\\n'。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/api/assistant/chat")
@limiter.limit("20/minute")          # AI 接口限流：每分钟最多 20 次，防烧钱
async def assistant_chat(request: Request, req: AssistantRequest):
    """个人助手问答：多智能体检索 → 轨迹回传 → LLM 流式作答。"""
    # 首次确保向量库已建（9 板块切块入库）
    await ai_api.ensure_index()
    llm = get_llm()
    store = ai_api._get_store()
    emb = ai_api.get_embeddings()

    # 1) 跑多智能体管线（内部会调 LLM / Embedding，是同步重活，丢到线程避免阻塞事件循环）
    pipe = await asyncio.to_thread(run_assistant_pipeline, req.message, store, emb, llm)

    # 2) 拼最终回答的上下文（检索资料 + 图谱关系）
    ctx_parts = []
    if pipe["doc_context"]:
        ctx_parts.append("【资料检索】\n" + pipe["doc_context"])
    if pipe["graph_context"]:
        ctx_parts.append(pipe["graph_context"])
    ctx = "\n\n".join(ctx_parts)

    prompt = (
        "你是「许成合」的个人 AI 助手，只能依据下方【资料】用中文回答访客的问题；"
        "资料里没有就如实说不知道，不要编造。语气自然、简洁。\n"
        f"【资料】\n{ctx}\n\n【问题】{req.message}\n【回答】"
    )

    def gen() -> AsyncIterator[str]:
        # 先发轨迹事件：前端据此渲染「思考过程」面板（查询改写/agent 分派/检索/rerank）
        yield _sse("trace", {
            "rewritten": pipe["rewritten"],
            "plan": pipe["plan"],
            "agent_trace": pipe["agent_trace"],
            "retrieval": pipe["retrieval_trace"],
            "graph_triples": pipe["graph_triples"],
        })
        # 再逐 token 流式生成最终回答
        try:
            for chunk in llm.stream(prompt):
                text = getattr(chunk, "content", "")
                if text:
                    yield _sse("token", text)
        except Exception as e:
            yield _sse("token", f"\n[生成出错：{e}]")
        yield _sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8")
