# ======================================================================
# 云端 LLM 客户端：RAG 回答、LangGraph 智能体都靠它生成文本
#
# 同样走 OpenAI 兼容接口，默认接 DeepSeek（deepseek-chat，便宜且强）。
# ======================================================================
from langchain_openai import ChatOpenAI

from app.core.config import get_settings


def get_llm(temperature: float = 0.3) -> ChatOpenAI:
    """构造一个 OpenAI 兼容的对话模型客户端（默认指向 DeepSeek）。

    temperature 控制随机性：RAG 问答用低一点更稳，创意生成可高一点。
    """
    s = get_settings()
    if not s.llm_api_key:
        raise RuntimeError("未配置 llm_api_key：请在 .env 填入 DeepSeek 等云端 LLM 的 Key。")
    return ChatOpenAI(
        model=s.llm_model,             # 模型名，例如 deepseek-chat
        api_key=s.llm_api_key,         # 云端密钥
        base_url=s.llm_base_url,       # OpenAI 兼容地址
        temperature=temperature,
    )
