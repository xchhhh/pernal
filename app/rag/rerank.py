# ======================================================================
# Rerank 重排模块（A7 管线的关键一环）
#
# 为什么需要 rerank：
#   混合检索（向量+BM25）用 RRF 融合后，候选虽然相关但「精排」不够准。
#   rerank 用一个更强的模型对候选逐对打分，把最相关的顶到最前，
#   直接决定最终喂给 LLM 的上下文质量——这是 RAG 效果好坏的分水岭。
#
# 两种后端（可在配置里切换）：
#   1) cross-encoder（默认）：用 bge-reranker-v2-m3 这类交叉编码器，
#      把「(query, doc)」拼一起过一次模型，直接输出相关性分数，最准。
#   2) llm：没有交叉编码器时，用 DeepSeek 对候选整体打分排序（降级方案）。
#
# 设计要点（资深视角）：交叉编码器模型较大，用「懒加载 + 进程内缓存」，
# 只在第一次真正需要 rerank 时才下载/载入，避免拖慢启动；
# 载入失败时自动降级到 llm 重排，保证「重排」这一环永远不空转。
# ======================================================================
from functools import lru_cache

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("rerank")

# 懒加载的交叉编码器单例（模块级缓存，避免重复占内存）
_cross_encoder = None
_cross_encoder_loaded = False  # 标记是否已尝试加载，避免每次都重试失败


def _load_cross_encoder():
    """懒加载交叉编码器模型（带镜像与失败降级）。

    返回 sentence_transformers.CrossEncoder 实例；若环境不支持/下载失败返回 None。
    """
    global _cross_encoder, _cross_encoder_loaded
    if _cross_encoder_loaded:
        return _cross_encoder  # 已尝试过：成功就有实例，失败就是 None，不再重试
    _cross_encoder_loaded = True
    s = get_settings()
    try:
        # 关键：国内 VPS 走 HF 镜像拉模型，否则 hf.co 常被墙导致超时
        import os
        os.environ.setdefault("HF_ENDPOINT", s.hf_endpoint)
        from sentence_transformers import CrossEncoder
        log.info("rerank.loading_cross_encoder", model=s.rerank_cross_encoder_model)
        _cross_encoder = CrossEncoder(s.rerank_cross_encoder_model)
        log.info("rerank.cross_encoder_ready")
    except Exception as e:
        # 载入失败（无网络/无 torch/内存不足等）：记日志，后续自动降级 llm 重排
        log.warning("rerank.cross_encoder_unavailable", error=str(e))
        _cross_encoder = None
    return _cross_encoder


def _rerank_by_cross_encoder(query: str, candidates: list[dict], top_n: int) -> list[dict]:
    """用交叉编码器对候选打分重排。candidates=[{id,text,metadata}]。"""
    ce = _load_cross_encoder()
    if ce is None:
        return None  # 交给调用方降级
    # 把 query 与各候选文本拼接成 (query, doc) 对，模型一次算出相关性分数
    pairs = [(query, c["text"]) for c in candidates]
    scores = ce.predict(pairs)  # 返回每个候选的相关性分数（越高越相关）
    # 把分数写回候选，再按分数降序排，取前 top_n
    for c, sc in zip(candidates, scores):
        c["rerank_score"] = float(sc)
    ranked = sorted(candidates, key=lambda c: c["rerank_score"], reverse=True)
    return ranked[:top_n]


def _rerank_by_llm(query: str, candidates: list[dict], top_n: int, llm) -> list[dict]:
    """降级方案：用 LLM 对候选整体排序，返回重排后的前 top_n。

    做法：把候选编号+摘要发给 LLM，让它返回「最相关的 top_n 个编号」的 JSON，
    再按这个顺序重组。只花一次 LLM 调用，成本可控。
    """
    if llm is None or not candidates:
        return candidates[:top_n]
    # 给候选编号，避免 LLM 杜撰内容；只发前 200 字摘要，控长度
    numbered = "\n".join(
        f"[{i}] {c['text'][:200]}" for i, c in enumerate(candidates)
    )
    prompt = (
        "你是检索重排器。下面是一组候选资料片段，按与问题的相关性从高到低排序，"
        "只返回最相关的编号列表（JSON 数组，如 [2,0,3]），不要解释。\n"
        f"问题：{query}\n候选：\n{numbered}\n最相关的编号（最多 {top_n} 个）："
    )
    try:
        resp = llm.invoke(prompt).content
        # 从返回里抠出 JSON 数组（容错：找不到就原样返回）
        import json, re
        m = re.search(r"\[.*?\]", resp, re.DOTALL)
        if not m:
            return candidates[:top_n]
        order = json.loads(m.group(0))
        picked = []
        for idx in order:
            if 0 <= idx < len(candidates) and candidates[idx] not in picked:
                picked.append(candidates[idx])
        # 没排满的用剩余候选补上
        for c in candidates:
            if c not in picked:
                picked.append(c)
        return picked[:top_n]
    except Exception as e:
        log.warning("rerank.llm_fallback_failed", error=str(e))
        return candidates[:top_n]


def rerank(query: str, candidates: list[dict], top_n: int, llm=None) -> list[dict]:
    """对外统一入口：对候选做重排，返回重排后的前 top_n 条。

    优先级：cross-encoder（准）→ llm（降级）→ 原样截断（兜底）。
    """
    if not candidates:
        return []
    s = get_settings()
    # 1) 首选交叉编码器
    if s.rerank_backend in ("cross-encoder", "auto"):
        ranked = _rerank_by_cross_encoder(query, candidates, top_n)
        if ranked is not None:
            return ranked
        log.info("rerank.fallback_to_llm")  # 交叉编码器不可用，降级
    # 2) 显式要求 llm，或交叉编码器降级
    if s.rerank_backend == "llm" or llm is not None:
        return _rerank_by_llm(query, candidates, top_n, llm)
    # 3) 兜底：直接截断
    return candidates[:top_n]
