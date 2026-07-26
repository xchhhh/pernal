# ======================================================================
# 云端 Embedding 客户端：把文本变成向量，存进 Chroma 供检索
#
# 设计要点（资深视角）：
#   - 走 OpenAI 兼容接口（langchain_openai.OpenAIEmbeddings），这样换供应商只改配置不改动代码；
#   - 默认接 DeepSeek（A2 决策）；若 DeepSeek 没有稳定的 embedding 接口，
#     把 .env 里的 embedding_base_url / embedding_model 改成「硅基流动 BGE」即可，本文件零改动；
#   - 调用发生在运行时（有 API key 之后），本函数只负责构造客户端。
# ======================================================================
from langchain_openai import OpenAIEmbeddings

from app.core.config import get_settings


def get_embeddings() -> OpenAIEmbeddings:
    """构造一个 OpenAI 兼容的 Embedding 客户端（默认指向 DeepSeek）。

    返回的对象可直接传给 LangChain / Chroma 当 embed_model 用。
    """
    s = get_settings()
    # 前置校验：没有 key 提前报清晰错误，而不是把异常抛到 openai 底层
    if not s.embedding_api_key:
        raise RuntimeError(
            "未配置 embedding_api_key：请在 .env 填入云端 Embedding 的 Key"
            "（DeepSeek 若不支持 embedding，把 embedding_base_url / embedding_model 改成硅基流动 BGE）。"
        )
    return OpenAIEmbeddings(
        model=s.embedding_model,       # 模型名（DeepSeek 待验证，不行就改 bge-large-zh）
        api_key=s.embedding_api_key,   # 云端密钥（生产走环境变量，绝不写死）
        base_url=s.embedding_base_url, # OpenAI 兼容地址，例如 https://api.deepseek.com/v1
    )
