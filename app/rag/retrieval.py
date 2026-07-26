# ======================================================================
# M1 RAG 管线（A7）：查询改写 → 混合召回 → RRF 融合 → rerank 重排 → 压缩
#
# 这一层是「编排」，把 store / embed / llm 串起来。
# 其中 RRF 融合是纯函数（可独立单测）；涉及 LLM 的步骤运行时才调用。
# ======================================================================
from app.core.config import get_settings
from app.rag.chunking import chunk_section, collapse_to_parents


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


def hyde_document(question: str, llm) -> str:
    """HyDE（Hypothetical Document Embeddings）：让 LLM 先「假想」一段答案文档。

    原理（业界常用召回增强）：问句「他会什么技术」和文档「技术栈：Python、
    FastAPI…」在向量空间里距离较远；但「假想答案」和真实文档都是**陈述句**，
    语义距离近得多。拿假想文档去做向量检索，Recall 显著提升。
    注意：假想内容可能是编的——它只用来「检索」，绝不进入最终上下文。
    """
    prompt = (
        "请针对下面的问题，写一段 80 字以内的「假想资料片段」——像简历或项目文档里"
        "会出现的陈述句，直接陈述可能的答案内容（编造具体名称也没关系，"
        "这段话只用于检索匹配，不会展示给用户）。不要解释，直接输出片段。\n"
        f"问题：{question}\n片段："
    )
    return llm.invoke(prompt).content.strip()[:300]


# ----------------------------------------------------------------------
# 3) rerank 重排（已落地为真实交叉编码器重排，见 app/rag/rerank.py）
# ----------------------------------------------------------------------
# 这里只做转发：真正的重排（cross-encoder / llm 降级）在 rerank 模块里实现，
# 既支持 bge-reranker 这类交叉编码器，也能在模型不可用时降级到 LLM 打分。
from app.rag.rerank import rerank as _rerank_impl


# ----------------------------------------------------------------------
# 4) 压缩：把候选块拼成受控长度的上下文
# ----------------------------------------------------------------------
def compress(candidates: list[dict], max_chars: int = 2000) -> str:
    """把候选块拼成上下文文本，超长则截断到 max_chars，控制喂给 LLM 的长度与成本。

    父子切分后，候选的 c['text'] 已是「父块」完整上下文（见 _collapse_to_parents），
    父块比旧碎片大，故默认上限从 1500 提到 2000，让 1~2 个父块完整进上下文。
    """
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
        "hyde": None,
        "vector_top": [],
        "bm25_top": [],
        "hyde_top": [],
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
    # (b) 多路召回：向量 + BM25 +（可选）HyDE 假想文档向量
    vec = store.vector_search(query, s.rag_top_k_vector)
    bm25 = store.bm25_search(query, s.rag_top_k_bm25)
    hyde_hits = []
    if s.rag_enable_hyde and llm is not None:
        try:
            hy = hyde_document(question, llm)
            trace["hyde"] = hy  # 记录假想文档，前端「思考过程」可展示
            hyde_hits = store.vector_search(hy, s.rag_top_k_vector)
        except Exception:
            hyde_hits = []  # HyDE 失败不影响主链路
    trace["vector_top"] = [{"id": r[0], "title": r[2].get("title", ""), "snippet": r[1][:80]} for r in vec]
    trace["bm25_top"] = [{"id": r[0], "title": r[2].get("title", ""), "snippet": r[1][:80]} for r in bm25]
    trace["hyde_top"] = [{"id": r[0], "title": r[2].get("title", ""), "snippet": r[1][:80]} for r in hyde_hits]
    # (c) RRF 融合：把多路排名转成 doc_id 列表（HyDE 命中作为第三路投票）
    ranked_lists = [[r[0] for r in vec], [r[0] for r in bm25]]
    if hyde_hits:
        ranked_lists.append([r[0] for r in hyde_hits])
    fused = rrf_merge(ranked_lists, k=s.rag_rrf_k)
    trace["rrf_order"] = fused
    # (d) 用融合顺序重组候选块（去重，保留文本/元数据）
    # 注意：向量侧元数据含 parent_text/parent_id（父子切分），BM25 侧不含；
    # 先填向量/HyDE、BM25 仅补缺，确保命中块的完整元数据不被覆盖掉。
    by_id = {}
    for r in list(vec) + list(hyde_hits):
        if r[0] not in by_id:
            by_id[r[0]] = {"id": r[0], "text": r[1], "metadata": dict(r[2])}
    for r in bm25:
        if r[0] not in by_id:
            by_id[r[0]] = {"id": r[0], "text": r[1], "metadata": dict(r[2])}
    ordered = [by_id[i] for i in fused if i in by_id]
    # 记录 rerank 之前的顺序（多展示几条，对比更直观）
    trace["rerank_before"] = [c["id"] for c in ordered[: s.rag_rerank_top_n * 2 + 1]]
    # (e) 可选 rerank 重排
    reranked = _rerank_impl(
        question, ordered, s.rag_rerank_top_n, llm=llm if s.rag_enable_rerank else None
    )
    # (f) 父子切分收口：命中的子块回退为父块（完整上下文）并按父块去重
    final = collapse_to_parents(reranked)
    trace["rerank_after"] = [c["id"] for c in final]
    # 评测用：最终排序的稳定 id 列表（优先 parent_id——父块是喂给 LLM 的单位；
    # 老数据无 parent 信息时退回子块 id）。eval 脚本据此算 Recall/MRR/NDCG。
    trace["ranked_ids"] = [
        (c.get("metadata") or {}).get("parent_id") or c["id"] for c in final
    ]
    # 溯源：从最终候选块提取去重后的资料来源（标题 + 板块 key），供前端展示
    sources, seen = [], set()
    for c in final:
        meta = c.get("metadata") or {}
        key = meta.get("section_key") or meta.get("doc_id") or ""
        if key and key not in seen:
            seen.add(key)
            sources.append({
                "title": meta.get("title") or meta.get("section_key") or "资料",
                "section": meta.get("section_key", ""),
            })
    trace["sources"] = sources
    return final, trace
