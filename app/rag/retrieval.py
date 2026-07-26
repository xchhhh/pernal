# ======================================================================
# M1 RAG 管线（A7）：查询改写 → 混合召回 → RRF 融合 → rerank 重排 → 压缩
#
# 这一层是「编排」，把 store / embed / llm 串起来。
# 其中 RRF 融合是纯函数（可独立单测）；涉及 LLM 的步骤运行时才调用。
# ======================================================================
from app.core.config import get_settings
from app.rag.chunking import chunk_section


# ----------------------------------------------------------------------
# 1) RRF 融合（Reciprocal Rank Fusion）—— 纯函数，不依赖任何外部服务
# ----------------------------------------------------------------------
def rrf_merge(ranked_lists: list[list[str]], k: int = 60) -> list[str]:
    """把多路召回的「排名列表」融合成一个去重后的排名。

    原理：每个文档的得分 = Σ 1/(k + 排名)。排名越靠前(越小)得分越高。
    ranked_lists 里每个元素是「按相关性从高到低的 doc_id 列表」。
    """
    scores: dict[str, float] = {}
    for ranks in ranked_lists:
        for i, doc_id in enumerate(ranks):
            # i 从 0 开始，第 1 名距离是 k+1，符合 RRF 标准公式
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + i + 1)
    # 按总分降序返回 doc_id 列表
    return sorted(scores, key=lambda d: scores[d], reverse=True)


# ----------------------------------------------------------------------
# 2) 查询改写（可选）：用 LLM 把短问题扩写/拆子问题，提升召回
# ----------------------------------------------------------------------
def rewrite_query(question: str, llm) -> str:
    """用 LLM 把用户问句改写成更利于检索的查询（扩写同义词、补上下文）。"""
    prompt = (
        "你是检索系统的查询改写器。把用户的疑问改写成一段利于向量/关键词检索的查询，"
        "保留关键信息，可适当扩写同义词，不要回答原问题。\n"
        f"原问题：{question}\n改写后："
    )
    return llm.invoke(prompt).content.strip()


# ----------------------------------------------------------------------
# 3) rerank 重排（已落地为真实交叉编码器重排，见 app/rag/rerank.py）
# ----------------------------------------------------------------------
# 这里只做转发：真正的重排（cross-encoder / llm 降级）在 rerank 模块里实现，
# 既支持 bge-reranker 这类交叉编码器，也能在模型不可用时降级到 LLM 打分。
from app.rag.rerank import rerank as _rerank_impl


# ----------------------------------------------------------------------
# 4) 压缩：把候选块拼成受控长度的上下文
# ----------------------------------------------------------------------
def compress(candidates: list[dict], max_chars: int = 1500) -> str:
    """把候选块拼成上下文文本，超长则截断到 max_chars，控制喂给 LLM 的长度与成本。"""
    parts = []
    total = 0
    for c in candidates:
        block = f"[{c['metadata'].get('title', '')}]\n{c['text']}"
        if total + len(block) > max_chars:
            # 剩下的空间不够整块，截断补齐
            remain = max(0, max_chars - total)
            if remain > 50:  # 至少留点有意义的内容
                parts.append(block[:remain] + "…")
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


# ----------------------------------------------------------------------
# 5) 端到端：把上面几步串成一条 RAG 检索链（供 api.py 调用）
# ----------------------------------------------------------------------
def retrieve(question: str, store, embeddings, llm=None) -> list[dict]:
    """执行「混合召回 → RRF → rerank → 压缩前」的检索，返回候选块列表。"""
    candidates, _ = retrieve_with_trace(question, store, embeddings, llm)
    return candidates


def retrieve_with_trace(question: str, store, embeddings, llm=None) -> tuple[list[dict], dict]:
    """和 retrieve 一样，但额外返回「检索轨迹」字典，供前端「思考过程」面板展示。

    轨迹包含：查询改写结果、向量召回 Top、BM25 召回 Top、RRF 融合顺序、rerank 前后排序对比。
    """
    s = get_settings()
    trace = {
        "rewritten": None,
        "vector_top": [],
        "bm25_top": [],
        "rrf_order": [],
        "rerank_before": [],
        "rerank_after": [],
    }
    # (a) 可选查询改写
    query = question
    if s.rag_enable_query_rewrite and llm is not None:
        try:
            query = rewrite_query(question, llm)
            trace["rewritten"] = query  # 记录改写后的查询，前端可展示「原问题→改写问题」
        except Exception:
            query = question  # 改写失败就退回原句，保证可用
    # (b) 两路召回
    vec = store.vector_search(query, s.rag_top_k_vector)
    bm25 = store.bm25_search(query, s.rag_top_k_bm25)
    trace["vector_top"] = [{"id": r[0], "title": r[2].get("title", ""), "snippet": r[1][:80]} for r in vec]
    trace["bm25_top"] = [{"id": r[0], "title": r[2].get("title", ""), "snippet": r[1][:80]} for r in bm25]
    # (c) RRF 融合：把两路排名转成 doc_id 列表
    vec_ids = [r[0] for r in vec]
    bm25_ids = [r[0] for r in bm25]
    fused = rrf_merge([vec_ids, bm25_ids], k=s.rag_rrf_k)
    trace["rrf_order"] = fused
    # (d) 用融合顺序重组候选块（去重，保留文本/元数据）
    by_id = {}
    for r in vec + bm25:
        by_id[r[0]] = {"id": r[0], "text": r[1], "metadata": r[2]}
    ordered = [by_id[i] for i in fused if i in by_id]
    # 记录 rerank 之前的顺序（多展示几条，对比更直观）
    trace["rerank_before"] = [c["id"] for c in ordered[: s.rag_rerank_top_n * 2 + 1]]
    # (e) 可选 rerank 重排
    reranked = _rerank_impl(
        question, ordered, s.rag_rerank_top_n, llm=llm if s.rag_enable_rerank else None
    )
    trace["rerank_after"] = [c["id"] for c in reranked]
    return reranked, trace
