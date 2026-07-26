# ======================================================================
# AI 相关的 HTTP 路由（#21 对外接口）
#
# 端点：
#   GET  /api/sections      取全部板块（前端渲染/调试用，无需 LLM）
#   GET  /api/graph-data    知识图谱可视化数据（Cytoscape 格式，无需 LLM）
#   POST /api/chat          M1 RAG 问答（流式，需要云端 LLM+Embedding）
#   POST /api/agent         M2 LangGraph Agent+MCP（流式，需要 LLM+MCP）
#   POST /api/multi-agent    M3 多 Agent 协作（流式，需要 LLM）
#
# 资深视角：AI 接口都加限流（烧钱接口严一点），且所有「重活」下沉到 rag/* 模块，
# 本文件只做「收请求 → 调服务 → 流式返回」。
# ======================================================================
import sys
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.limiter import limiter
from app.rag import graph as graph_mod
from app.rag.agent import build_agent, local_tools, run_assistant_pipeline
from app.rag.chunking import chunk_section
from app.rag.embed import get_embeddings
from app.rag.llm import get_llm
from app.rag.retrieval import compress, retrieve
from app.rag.store import HybridStore
from app.services import content

router = APIRouter()


# ---------- 请求体 ----------
class ChatRequest(BaseModel):
    """/api/chat 的请求体：访客的提问。"""

    question: str


# ---------- 工具函数 ----------
_store = None  # 进程内缓存 HybridStore，避免每次请求重建 Chroma 客户端


def _get_store() -> HybridStore:
    """懒加载并返回混合检索存储（首次用真实云端 embedding 初始化）。"""
    global _store
    if _store is None:
        emb = get_embeddings()
        # embed_documents 是 OpenAIEmbeddings 的批量向量化方法，正好匹配 store 的 embed_fn 签名
        _store = HybridStore(embed_fn=emb.embed_documents)
    return _store


def ensure_index() -> None:
    """首次使用时把 9 板块切块写进向量库+BM25；已建过就跳过。"""
    store = _get_store()
    if store._collection.count() > 0:
        return
    sections = content.get_all_sections()
    s = get_settings()
    docs = []
    for key, sec in sections.items():
        docs.extend(chunk_section(key, sec.get("title", key), sec.get("body"),
                                  chunk_size=s.rag_chunk_size, overlap=s.rag_chunk_overlap))
    store.add_documents(docs)


async def get_mcp_tools():
    """通过 stdio 拉起同容器的 MCP server，取回 10 个工具（A3 真·MCP）。

    返回 langchain 可用的 tool 列表；若启动失败则回退到本地工具，保证不崩。
    """
    try:
        mcp_file = str(Path(__file__).with_name("mcp_server.py"))
        client = MultiServerMCPClient({
            "portal": {
                "command": sys.executable,       # 复用同一个 Python 解释器
                "args": [mcp_file],               # 直接跑 mcp_server.py（内含 mcp.run()）
                "transport": "stdio",             # 同容器 stdio，用完即走
            }
        })
        return await client.get_tools()
    except Exception:
        # MCP 拉起失败（如缺依赖）时，退回本地工具，Agent 仍可用
        return local_tools()


# ---------- 端点：无需 LLM ----------
@router.get("/api/sections")
async def api_sections():
    """返回全部板块内容（前端调试/渲染用）。"""
    return JSONResponse(content.get_all_sections())


@router.get("/api/graph-data")
async def api_graph():
    """返回知识图谱的 Cytoscape 格式数据（前端可视化用）。"""
    return JSONResponse(graph_mod.to_cytoscape())


# ---------- 端点：M1 RAG 问答（流式） ----------
@router.post("/api/chat")
@limiter.limit("20/minute")          # AI 接口严限流：每分钟最多 20 次，防烧钱
async def api_chat(request: Request, req: ChatRequest):
    """RAG 问答：检索 → 拼上下文 → 让 LLM 基于资料回答，逐字流式返回。"""
    ensure_index()
    llm = get_llm()
    store = _get_store()
    # 1) 检索候选块（含查询改写/RRF/可选重排）
    candidates = retrieve(req.question, store, get_embeddings(), llm)
    # 2) 压缩成受控长度的上下文
    ctx = compress(candidates) if get_settings().rag_compress else "\n\n".join(c["text"] for c in candidates)
    # 3) 构造提示：限定只依据资料回答，避免幻觉
    prompt = (
        "你是个人门户的 AI 助手，只能依据下方【资料】回答，资料里没有就如实说不知道。\n"
        f"【资料】\n{ctx}\n\n【问题】{req.question}\n【回答】"
    )

    def gen() -> AsyncIterator[str]:
        # llm.stream 返回 token 迭代器，逐个 yield 实现打字机效果
        for chunk in llm.stream(prompt):
            yield chunk.content

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


# ---------- 端点：M2 LangGraph Agent + MCP（流式） ----------
@router.post("/api/agent")
@limiter.limit("10/minute")
async def api_agent(request: Request, req: ChatRequest):
    """单 Agent：绑定 MCP 工具，自己决定调哪个工具回答问题。"""
    llm = get_llm()
    tools = await get_mcp_tools()          # 接入 10 个 MCP 工具
    agent = build_agent(llm, tools)
    result = agent.invoke({"messages": [("user", req.question)]})

    def gen() -> AsyncIterator[str]:
        # 取最后一条 AI 消息作为最终回答
        msg = result["messages"][-1]
        yield getattr(msg, "content", str(msg))

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


# ---------- 端点：M3 多智能体协作（流式，走统一管线） ----------
@router.post("/api/multi-agent")
@limiter.limit("10/minute")
async def api_multi_agent(request: Request, req: ChatRequest):
    """多智能体：主管分派检索员/图谱员协作，最后流式生成回答（与助手共用管线）。"""
    await ensure_index()
    llm = get_llm()
    store = _get_store()
    emb = get_embeddings()
    pipe = run_assistant_pipeline(req.question, store, emb, llm)
    ctx_parts = []
    if pipe["doc_context"]:
        ctx_parts.append("【资料检索】\n" + pipe["doc_context"])
    if pipe["graph_context"]:
        ctx_parts.append(pipe["graph_context"])
    ctx = "\n\n".join(ctx_parts)
    prompt = (
        "你是「许成合」的个人 AI 助手，只能依据下方【资料】用中文回答；"
        "资料里没有就如实说不知道。语气自然简洁。\n"
        f"【资料】\n{ctx}\n\n【问题】{req.question}\n【回答】"
    )

    def gen() -> AsyncIterator[str]:
        try:
            for chunk in llm.stream(prompt):
                text = getattr(chunk, "content", "")
                if text:
                    yield text
        except Exception as e:
            yield f"\n[生成出错：{e}]"

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")
