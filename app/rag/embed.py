# ======================================================================
# 文本向量化客户端：把文本变成向量，存进 Chroma 供检索
#
# 设计要点（资深视角）：
#   - 提供「云端 / 本地」两种后端，由配置 embedding_backend 切换：
#       * cloud ：走 OpenAI 兼容接口（langchain_openai.OpenAIEmbeddings），
#                 适合你有可用「文本」embedding 模型的场景；
#       * local ：用 sentence-transformers 在本地向量化（默认，契合本项目），
#                 不依赖账号模型权限，也不怕网络抖动把检索链路卡死。
#   - 本地模型做模块级单例缓存，避免每次请求重复加载占内存。
#   - 对外只暴露 embed_documents(texts)->list[list[float]]（store 的 embed_fn 签名），
#     无论云端还是本地，上层代码零改动。
# =====================================================================
import os

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("embed")

# 本地模型单例（模块级缓存，避免重复加载占内存）
_local_model = None
_local_loaded = False


class LocalEmbeddings:
    """本地文本向量化（sentence-transformers），兼容 store 的 embed_fn 签名。

    提供 embed_documents(texts)->list[list[float]] 与 embed_query(text)->list[float]。
    """

    def __init__(self, model_name: str):
        # 关键：国内拉模型走 HF 镜像，否则 hf.co 常被墙导致超时
        os.environ.setdefault("HF_ENDPOINT", get_settings().hf_endpoint)
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)

    def embed_documents(self, texts) -> list:
        """批量向量化：输入字符串列表，返回向量列表（已归一化到单位长度）。"""
        emb = self._model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        )
        return emb.tolist()

    def embed_query(self, text: str) -> list:
        """单条查询向量化（retrieve 里也会用到）。"""
        return self.embed_documents([text])[0]


def _get_local() -> LocalEmbeddings | None:
    """懒加载并缓存本地 embedding 模型；失败返回 None。"""
    global _local_model, _local_loaded
    if _local_loaded:
        return _local_model  # 已尝试过：成功就有实例，失败就是 None，不再重试
    _local_loaded = True
    s = get_settings()
    try:
        log.info("embed.loading_local", model=s.local_embedding_model)
        _local_model = LocalEmbeddings(s.local_embedding_model)
        log.info("embed.local_ready")
    except Exception as e:
        # 载入失败（无网络/无 torch/内存不足等）：记日志，后续调用会抛清晰错误
        log.error("embed.local_failed", error=str(e))
        _local_model = None
    return _local_model


def get_embeddings():
    """返回兼容 store embed_fn 签名的向量化对象（必须有 embed_documents 方法）。

    优先级：cloud（若配置了且模型可用）→ local（默认兜底）。
    """
    s = get_settings()
    # 1) 先尝试云端（仅当你有可用「文本」embedding 模型时）
    if s.embedding_backend == "cloud" and s.embedding_api_key and s.embedding_model:
        try:
            from langchain_openai import OpenAIEmbeddings

            log.info("embed.use_cloud", model=s.embedding_model)
            return OpenAIEmbeddings(
                model=s.embedding_model,
                api_key=s.embedding_api_key,
                base_url=s.embedding_base_url,
            )
        except Exception as e:
            log.warning("embed.cloud_failed_fallback_local", error=str(e))
    # 2) 本地兜底（默认）
    local = _get_local()
    if local is None:
        raise RuntimeError(
            "本地 embedding 模型加载失败：请检查网络/HF 镜像或模型名"
            f"（当前模型={s.local_embedding_model}）"
        )
    return local
