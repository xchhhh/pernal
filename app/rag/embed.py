# ======================================================================
# 文本向量化客户端：把文本变成向量，存进 Chroma 供检索
#
# 设计要点（资深视角）：
#   - 提供「云端 / 本地」两种后端，由配置 embedding_backend 切换：
#       * cloud ：走火山引擎 /embeddings/multimodal 端点（文本-only 对象格式）。
#                 注意：豆包视觉多模态模型不支持 OpenAI 风格 /embeddings 的纯字符串，
#                 必须走 multimodal 端点，且 input 用 [{type:text,text:...}] 对象；
#                 整段 input 会被合成「一个」向量，因此批量嵌入需逐条调用。
#       * local ：用 sentence-transformers 在本地向量化（离线兜底，不依赖账号/网络）。
#   - 本地模型做模块级单例缓存，避免重复加载占内存。
#   - 对外只暴露 embed_documents(texts)->list[list[float]]（store 的 embed_fn 签名），
#     无论云端还是本地，上层代码零改动。
# =====================================================================
import json
import os
import time
import urllib.request
from typing import List

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("embed")

# 本地模型单例（模块级缓存，避免重复加载占内存）
_local_model = None
_local_loaded = False


# ----------------------------------------------------------------------
# 云端后端：火山引擎 multimodal embedding（文本-only）
# ----------------------------------------------------------------------
class VolcanoMultimodalEmbeddings:
    """火山引擎 /embeddings/multimodal 文本嵌入后端。

    为什么不用 OpenAIEmbeddings：豆包视觉模型不在 OpenAI 兼容的 /embeddings
    接口上接受纯字符串（会 400）。它只认 multimodal 端点，input 必须是
    [{type:text,text:...}] 这样的对象数组。

    注意批量语义：整段 input 数组会被合成「一个」向量。所以给多段文本嵌入时，
    必须逐条调用（每段一次请求），不能把多段塞进一个 input 数组。
    """

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 60):
        # base_url 形如 https://ark.cn-beijing.volces.com/api/v3
        # multimodal 端点 = base + /embeddings/multimodal
        self.endpoint = base_url.rstrip("/") + "/embeddings/multimodal"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _embed_one(self, text: str) -> List[float]:
        """对单条文本调一次 multimodal 端点，返回向量（带简单重试）。"""
        payload = {
            "model": self.model,
            "input": [{"type": "text", "text": text}],
        }
        data = json.dumps(payload).encode("utf-8")
        last_err = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    self.endpoint, data=data, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                # 单条 input 时 data 是 {embedding, object}
                return body["data"]["embedding"]
            except Exception as e:  # noqa: 网络/限流/超时都可能，统一重试
                last_err = e
                log.warning("embed.cloud_retry", attempt=attempt + 1, error=str(e))
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"火山 embedding 调用失败：{last_err}")

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """批量向量化：逐条调用（见类注释，批量会被合成单向量）。"""
        return [self._embed_one(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        """单条查询向量化（retrieve 里也会用到）。"""
        return self._embed_one(text)


# ----------------------------------------------------------------------
# 本地后端：sentence-transformers（离线兜底）
# ----------------------------------------------------------------------
class LocalEmbeddings:
    """本地文本向量化（sentence-transformers），兼容 store 的 embed_fn 签名。"""

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

    优先级：cloud（已配置且模型可用）→ local（离线兜底）。
    cloud 走火山 /embeddings/multimodal 文本-only 接口，呼应「云端 AI 联调」需求。
    """
    s = get_settings()
    # 1) 云端（默认）：火山 multimodal 文本嵌入
    if s.embedding_backend == "cloud" and s.embedding_api_key and s.embedding_model:
        log.info("embed.use_cloud", model=s.embedding_model)
        return VolcanoMultimodalEmbeddings(
            api_key=s.embedding_api_key,
            base_url=s.embedding_base_url,
            model=s.embedding_model,
        )
    # 2) 本地兜底（默认离线场景）
    local = _get_local()
    if local is None:
        raise RuntimeError(
            "本地 embedding 模型加载失败：请检查网络/HF 镜像或模型名"
            f"（当前模型={s.local_embedding_model}）"
        )
    return local
