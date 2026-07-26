# ======================================================================
# #21 AI 模块验证脚本（pytest 可直接跑）
#
# 覆盖四大 AI 模块中「不依赖云端 key」的部分：
#   - M1 切块 / RRF 融合（纯逻辑）
#   - M4 知识图谱构建 / 多跳 / 可视化导出
#   - 混合检索存储：用确定性假 embedding 测 Chroma 写入与查询（不烧钱）
#   - MCP server：列出 10 个工具
#   - LangGraph：M2/M3 图能正确编译
#   - 应用装配：/api/sections、/api/graph-data 等无需 LLM 的端点真机返回
#
# 注：/api/chat、/api/agent、/api/multi-agent 的流式回答需要云端 LLM key，
#     填好 .env 的 llm_api_key / embedding_api_key 后才有真实输出。
# ======================================================================
import asyncio
import hashlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# ---- 在导入任何 app 模块前，把向量库/数据库指到临时目录，避免污染真数据 ----
_TMP = tempfile.mkdtemp(prefix="portal_test_")
os.environ["CHROMA_PERSIST_DIR"] = os.path.join(_TMP, "chroma")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"

from app.core.config import get_settings          # noqa: E402
from app.core.db import SessionLocal, init_db      # noqa: E402
from app.data.models import GraphTriple            # noqa: E402
from app.data import seed                          # noqa: E402
from app.rag import graph as graph_mod             # noqa: E402
from app.rag.chunking import chunk_section         # noqa: E402
from app.rag.retrieval import rrf_merge            # noqa: E402
from app.rag.store import HybridStore              # noqa: E402
from app.rag.mcp_server import mcp as mcp_server   # noqa: E402
from app.rag.agent import build_agent, build_supervisor, local_tools  # noqa: E402
from app.rag.llm import get_llm                     # noqa: E402
from langchain_core.language_models.fake_chat_models import FakeListChatModel  # noqa: E402


class FakeBindToolsChatModel(FakeListChatModel):
    """FakeListChatModel 默认不支持 bind_tools（LangChain 1.x 构建 agent 时会调）。

    这里覆写 bind_tools 直接返回自身，仅为验证「图能编译」，不真正调 LLM。
    """

    def bind_tools(self, tools, **kwargs):
        return self


# 准备一个独立测试库：建表 + 种子（含图谱三元组）
@pytest.fixture(scope="module", autouse=True)
def _prepare_db():
    get_settings().database_url = os.environ["DATABASE_URL"]
    init_db()
    seed.seed_if_empty()        # 写入 9 板块 + 图谱三元组占位
    yield


# ---------------- M1：切块 ----------------
def test_chunk_section_returns_metadata():
    docs = chunk_section("skills", "技术栈", {"languages": ["Python", "SQL"]})
    assert len(docs) >= 1
    assert docs[0]["metadata"]["section_key"] == "skills"
    assert "doc_id" in docs[0]["metadata"]


# ---------------- M1：RRF 融合（纯逻辑） ----------------
def test_rrf_merge_fuses_rankings():
    # 两路召回都排第一的文档，融合后应该还是第一
    fused = rrf_merge([["a", "b", "c"], ["a", "c", "b"]], k=60)
    assert fused[0] == "a"
    # 出现越多路、排名越靠前的，分越高
    fused2 = rrf_merge([["x", "y"], ["x", "z"], ["x", "w"]])
    assert fused2[0] == "x"


# ---------------- 混合检索：假 embedding 验证 Chroma 写入/查询 ----------------
def _fake_embed(texts):
    """确定性假 embedding：同一文本永远得到同一向量，便于无 key 验证 Chroma。"""
    out = []
    for t in texts:
        digest = hashlib.sha256(t.encode("utf-8")).digest()
        vec = [(b / 255.0 - 0.5) for b in digest[:32]]  # 32 维
        out.append(vec)
    return out


def test_hybrid_store_add_and_vector_search():
    store = HybridStore(collection_name="test_vec", embed_fn=_fake_embed)
    store.add_documents([
        {"text": "Python 用于 AI 应用开发", "metadata": {"section_key": "skills", "title": "技术栈", "doc_id": "skills::0"}},
        {"text": "FastAPI 是 Web 框架", "metadata": {"section_key": "skills", "title": "技术栈", "doc_id": "skills::1"}},
    ])
    res = store.vector_search("Python AI", k=1)
    assert len(res) == 1
    # 假 embedding 非语义，只验证「返回的是已索引的文档」（向量检索链路通）
    assert res[0][1] in {"Python 用于 AI 应用开发", "FastAPI 是 Web 框架"}


# ---------------- M4：知识图谱 ----------------
def test_graph_build_and_cytoscape():
    g = graph_mod.build_graph()
    assert g.number_of_nodes() > 0
    # “你的名字”应连出多条关系
    nb = graph_mod.neighbors("你的名字")
    assert len(nb) >= 5
    cyto = graph_mod.to_cytoscape(g)
    assert "elements" in cyto
    assert any(e["data"].get("source") for e in cyto["elements"])


def test_graph_multi_hop():
    sub = graph_mod.multi_hop("你的名字", max_depth=2)
    assert sub.number_of_nodes() >= 1


# ---------------- MCP server：10 个工具 ----------------
def test_mcp_lists_ten_tools():
    tools = asyncio.run(mcp_server.list_tools())
    names = {t.name for t in tools}
    assert len(tools) >= 10
    for expected in ["get_basics", "get_projects", "compute_skill_match", "get_graph_relations"]:
        assert expected in names


# ---------------- LangGraph：M2/M3 图能编译 ----------------
def test_agent_graph_compiles():
    fake_llm = FakeBindToolsChatModel(responses=["done"])
    agent = build_agent(fake_llm, local_tools())
    assert hasattr(agent, "invoke")          # 编译后的图才有 invoke
    sup = build_supervisor(fake_llm, local_tools())
    assert hasattr(sup, "invoke")


# ---------------- 应用装配：非 LLM 端点真机返回 ----------------
def test_app_endpoints_no_llm():
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/ready").status_code == 200
        r = c.get("/api/sections")
        assert r.status_code == 200
        assert "basics" in r.json()
        rg = c.get("/api/graph-data")
        assert rg.status_code == 200
        assert "elements" in rg.json()
