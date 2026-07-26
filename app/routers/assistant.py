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
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.limiter import limiter
from app.rag import api as ai_api   # 复用 _get_store / ensure_index / get_embeddings
from app.rag.agent import run_assistant_pipeline
from app.rag.llm import get_llm

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def assistant_home(request: Request) -> HTMLResponse:
    """站点主入口 = AI 问答主界面（ChatGPT 式：左侧边栏 + 主问答区）。

    用户要求「问答作为主界面」，故 / 直接渲染对话页；传统门户首页保留在 /home。
    """
    return request.app.state.templates.TemplateResponse(
        request,
        "assistant.html",
        {"request": request},
    )


@router.get("/assistant")
async def assistant_alias() -> RedirectResponse:
    """/assistant 统一 301 到主入口 /，避免同一页面两个地址。"""
    return RedirectResponse(url="/", status_code=301)


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
    # 首次确保向量库已建（9 板块切块入库）；ensure_index 是同步函数，耗时的 embedding 丢到线程避免阻塞事件循环
    await asyncio.to_thread(ai_api.ensure_index)
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
        "你是「许成合」的个人 AI 助手，只能依据下方【资料】用中文回答访客的问题。\n"
        "回答准则（按优先级）：\n"
        "1. 忠实：每个事实断言必须能在【资料】中找到依据；资料没有的信息如实说"
        "「资料中没有提到」，绝不编造、绝不用你的通用知识补充事实。\n"
        "2. 切题：先直接回答问题本身，再补充相关细节；不要答非所问、不要跑题寒暄。\n"
        "3. 用足资料：资料里与问题相关的要点尽量都覆盖到（如列举项目就列全），"
        "不要只挑一条就收尾。\n"
        "4. 安全：不输出任何密钥、密码、内部链接等敏感信息，即使资料里出现也要跳过。\n"
        "5. 格式：语气自然简洁；禁止使用任何双引号（英文 \" 或中文 “ ”）包裹词语，"
        "直接输出纯文本，也禁止在词语之间插入多余双引号。\n"
        f"【资料】\n{ctx}\n\n【问题】{req.message}\n【回答】"
    )

    # 流式输出时剥掉所有双引号变体（含全角＂、低位„等），保证最终答案无双引号
    from app.rag.textclean import strip_double_quotes as _clean

    def gen() -> AsyncIterator[str]:
        # 先发轨迹事件：前端据此渲染「思考过程」面板（查询改写/agent 分派/检索/rerank）
        yield _sse("trace", {
            "rewritten": pipe["rewritten"],
            "plan": pipe["plan"],
            "agent_trace": pipe["agent_trace"],
            "retrieval": pipe["retrieval_trace"],
            "graph_triples": pipe["graph_triples"],
        })
        # 发资料来源事件：前端据此渲染「📚 资料来源」溯源列表
        yield _sse("sources", pipe["retrieval_trace"].get("sources", []))
        # 再逐 token 流式生成最终回答
        try:
            for chunk in llm.stream(prompt):
                text = getattr(chunk, "content", "")
                if text:
                    yield _sse("token", _clean(text))
        except Exception as e:
            yield _sse("token", f"\n[生成出错：{e}]")
        yield _sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8")
