# ======================================================================
# 多智能体协同（M3 / 助手核心）
#
# 设计：一个「主管(supervisor)」根据问题类型，把任务分派给不同的「工人 agent」协作：
#   - retrieval（检索员）：跑完整 RAG 管线（查询改写→混合召回→RRF→rerank→压缩），
#     负责「从简历/项目资料里找答案」。
#   - graph（图谱员）：在知识图谱里查实体关系，负责「许成合会哪些技术 / 做过什么项目」这类关系问题。
#   - 两者都可由主管同时调用（协同），最后在主流程里汇总结论。
#
# 用 LangGraph 的 StateGraph 把这几个角色串成一张可编译、可追踪的图；
# 每个节点把「自己做了什么」写进 agent_trace，前端就能在「思考过程」里看到多 agent 怎么协作。
# ======================================================================
import json
import re
from functools import lru_cache
from typing import TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from app.core.logging import get_logger
from app.rag import graph as graph_mod
from app.rag.retrieval import compress, retrieve_with_trace, rewrite_query
from app.services import content

log = get_logger("agent")


# ----------------------------------------------------------------------
# 一组「本地工具」：供 M2 单 agent / MCP 演示用，保证没有 MCP 进程也能跑
# ----------------------------------------------------------------------
@tool
def list_sections() -> list[str]:
    """列出所有内容板块的 key（如 basics/projects/skills）。"""
    return list(content.get_all_sections().keys())


@tool
def graph_neighbors(entity: str) -> list[dict]:
    """查询知识图谱中某实体的直接关系。"""
    return graph_mod.neighbors(entity)


def local_tools():
    """返回本地工具列表（LangGraph agent 可直接绑定）。"""
    return [list_sections, graph_neighbors]


# ----------------------------------------------------------------------
# M2：单个 ReAct agent（绑定工具，自己决定调哪个工具）
# ----------------------------------------------------------------------
def build_agent(llm, tools=None):
    """构建一个绑定了工具的 ReAct agent。tools 缺省用本地工具。"""
    use_tools = tools if tools is not None else local_tools()
    try:
        from langchain.agents import create_agent as _ca
    except Exception:
        from langgraph.prebuilt import create_react_agent as _ca
    return _ca(llm, use_tools)


# ----------------------------------------------------------------------
# 助手多智能体状态：整张图在各节点间传递的就是这个字典
# ----------------------------------------------------------------------
class AssistantState(TypedDict):
    question: str          # 用户原问题
    rewritten: str         # 查询改写后的问题
    plan: list             # 主管决定的工人列表，如 ["retrieval","graph"]
    doc_context: str       # 检索员产出的上下文文本
    graph_context: str     # 图谱员产出的上下文文本
    retrieval_trace: dict   # 检索轨迹（向量/BM25/RRF/rerank）
    graph_triples: list    # 图谱命中的三元组
    agent_trace: list      # 给前端看的「分步日志」（多 agent 协作过程）
    llm: object            # 云端 LLM 客户端（从初始状态注入）
    store: object          # 混合检索存储（从初始状态注入）
    embeddings: object     # Embedding 客户端（从初始状态注入）
    messages: list         # LangGraph 约定的消息列表


# ----------------------------------------------------------------------
# 各节点：每个节点只干一件事，并把「做了什么」记进 agent_trace
# ----------------------------------------------------------------------
def _rewrite_node(state: AssistantState) -> dict:
    """节点1：查询改写员——把口语化短问题扩写成利于检索的查询。"""
    llm = state["llm"]
    try:
        rewritten = rewrite_query(state["question"], llm)
    except Exception:
        rewritten = state["question"]
    trace = list(state.get("agent_trace") or [])
    trace.append({"agent": "查询改写", "detail": rewritten or state["question"]})
    return {"rewritten": rewritten, "agent_trace": trace,
            "messages": [HumanMessage(content=state["question"]), AIMessage(content=rewritten)]}


def _supervisor_node(state: AssistantState) -> dict:
    """节点2：主管——读问题，决定该派哪些工人上（检索员 / 图谱员 / 都不用）。"""
    llm = state["llm"]
    q = state.get("rewritten") or state["question"]
    prompt = (
        "你是多智能体团队的主管。根据问题判断需要哪些工人来回答：\n"
        "- retrieval：需要从简历/项目资料里找事实（如经历、技能细节、项目内容）\n"
        "- graph：需要查实体间的关系（如'会哪些技术''做过什么项目''毕业于哪'）\n"
        "只返回 JSON：{\"workers\":[\"retrieval\"|\"graph\"]}。可同时选多个。\n"
        f"问题：{q}\nJSON："
    )
    plan = ["retrieval"]  # 默认至少检索，保证有资料可答
    try:
        resp = llm.invoke(prompt).content
        m = re.search(r"\{.*\}", resp, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            ws = data.get("workers") or []
            if isinstance(ws, list) and ws:
                plan = [w for w in ws if w in ("retrieval", "graph")]
                if not plan:
                    plan = ["retrieval"]
    except Exception as e:
        log.warning("agent.supervisor_parse_failed", error=str(e))
    trace = list(state.get("agent_trace") or [])
    trace.append({"agent": "主管", "detail": f"分派工人：{', '.join(plan)}"})
    return {"plan": plan, "agent_trace": trace}


def _retrieval_node(state: AssistantState) -> dict:
    """节点3：检索员——跑完整 RAG 管线，产出最相关的上下文 + 检索轨迹。"""
    store = state["store"]
    emb = state["embeddings"]
    llm = state["llm"]
    q = state.get("rewritten") or state["question"]
    candidates, rtrace = retrieve_with_trace(q, store, emb, llm)
    ctx = compress(candidates) if candidates else ""
    trace = list(state.get("agent_trace") or [])
    trace.append({
        "agent": "检索员",
        "detail": f"向量召回 {len(rtrace['vector_top'])} 条 + BM25 召回 {len(rtrace['bm25_top'])} 条"
                  f" → RRF 融合 → rerank 保留 {len(candidates)} 条",
    })
    return {"doc_context": ctx, "retrieval_trace": rtrace, "agent_trace": trace}


def _graph_node(state: AssistantState) -> dict:
    """节点4：图谱员——在知识图谱里查相关三元组，产出关系上下文。"""
    q = state.get("rewritten") or state["question"]
    triples = graph_mod.search_triples(q)
    lines = [f"{t['subject']} —{t['relation']}→ {t['obj']}" for t in triples[:8]]
    ctx = "知识图谱相关关系：\n" + "\n".join(lines) if lines else ""
    trace = list(state.get("agent_trace") or [])
    trace.append({"agent": "图谱员", "detail": f"命中 {len(triples)} 条实体关系"})
    return {"graph_context": ctx, "graph_triples": triples[:8], "agent_trace": trace}


# ----------------------------------------------------------------------
# 编译图（带条件路由，支持「同时派多个工人」）
# ----------------------------------------------------------------------
@lru_cache
def _build_graph():
    """编译多智能体图（节点 + 边），只编一次缓存复用。"""
    builder = StateGraph(AssistantState)
    builder.add_node("rewrite", _rewrite_node)
    builder.add_node("supervisor", _supervisor_node)
    builder.add_node("retrieval", _retrieval_node)
    builder.add_node("graph", _graph_node)

    builder.add_edge(START, "rewrite")
    builder.add_edge("rewrite", "supervisor")

    # 主管之后：优先去检索员；若计划里没有检索，则去图谱员；都没有则结束
    def _route_super(state):
        plan = state.get("plan") or []
        if "retrieval" in plan:
            return "retrieval"
        if "graph" in plan:
            return "graph"
        return END

    def _route_after_retrieval(state):
        # 检索员做完了：如果计划还要图谱员，就去；否则结束
        if "graph" in (state.get("plan") or []):
            return "graph"
        return END

    builder.add_conditional_edges("supervisor", _route_super,
                                  {"retrieval": "retrieval", "graph": "graph", END: END})
    builder.add_conditional_edges("retrieval", _route_after_retrieval,
                                  {"graph": "graph", END: END})
    builder.add_edge("graph", END)
    return builder.compile()


def run_assistant_pipeline(question: str, store, embeddings, llm) -> dict:
    """跑一遍多智能体管线，返回汇总的上下文与轨迹。

    返回字典含：rewritten / plan / doc_context / graph_context /
    retrieval_trace / graph_triples / agent_trace。
    最终「综合回答」由调用方（助手接口）用 LLM 流式生成。
    """
    graph = _build_graph()
    init = {
        "question": question,
        "rewritten": "",
        "plan": [],
        "doc_context": "",
        "graph_context": "",
        "retrieval_trace": {},
        "graph_triples": [],
        "agent_trace": [],
        "llm": llm,
        "store": store,
        "embeddings": embeddings,
        "messages": [],
    }
    result = graph.invoke(init)
    # 只取我们需要的字段返回，避免把 llm/store 等对象泄漏出去
    return {
        "rewritten": result.get("rewritten", ""),
        "plan": result.get("plan", []),
        "doc_context": result.get("doc_context", ""),
        "graph_context": result.get("graph_context", ""),
        "retrieval_trace": result.get("retrieval_trace", {}),
        "graph_triples": result.get("graph_triples", []),
        "agent_trace": result.get("agent_trace", []),
    }
