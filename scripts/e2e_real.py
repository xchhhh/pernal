# ======================================================================
# 本地验证脚本（真实简历内容）：离线验证 RAG 检索链路 + 云端密钥探针
#
# 说明：
#   - 离线段用「确定性假 embedding」替代云端，验证 切块→索引(Chroma+FTS5)→RRF→压缩
#     在「真实简历内容」上能正确执行（不联网，沙箱也能跑）。
#   - 云端段探测 DeepSeek/火山引擎 是否可用；若沙箱无外网会失败，属正常，
#     请在你的 VPS（有外网）上跑本脚本做最终联调。
# 运行：在 portfolio/ 目录下 `python scripts/e2e_real.py`（自动读 .env）
# ======================================================================
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.db import init_db, Base, engine
from app.data.seed import seed_if_empty
from app.services.content import get_all_sections
from app.rag.chunking import chunk_section
from app.rag.store import HybridStore
from app.rag.retrieval import retrieve
from app.rag.embed import get_embeddings
from app.rag.llm import get_llm


def fake_embed(texts):
    """确定性假 embedding（哈希），仅离线验证用，不联网。"""
    import hashlib
    dim = 32
    out = []
    for t in texts:
        vec = []
        for i in range(dim):
            h = hashlib.md5(f"{t}::{i}".encode()).hexdigest()
            vec.append(int(h[:8], 16) / 0xFFffffff * 2 - 1)
        out.append(vec)
    return out


def memory_mb():
    """读当前进程常驻内存（MB）。优先 psutil，失败用 Windows API。"""
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        import ctypes
        import ctypes.wintypes as wt

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_ulonglong), ("WorkingSetSize", ctypes.c_ulonglong),
                ("QuotaPeakPagedPoolUsage", ctypes.c_ulonglong), ("QuotaPagedPoolUsage", ctypes.c_ulonglong),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_ulonglong), ("QuotaNonPagedPoolUsage", ctypes.c_ulonglong),
                ("PagefileUsage", ctypes.c_ulonglong), ("PeakPagefileUsage", ctypes.c_ulonglong),
            ]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb
        )
        return pmc.WorkingSetSize / 1024 / 1024


def main():
    # 0) 重置库并写入真实简历内容
    Base.metadata.drop_all(engine)
    init_db()
    seed_if_empty()
    print("[1] 真实简历种子板块数 =", len(get_all_sections()))

    # 2) 离线构建混合索引（假 embedding，不联网）
    store = HybridStore(embed_fn=fake_embed)
    secs = get_all_sections()
    docs = []
    for k, v in secs.items():
        docs.extend(chunk_section(k, v["title"], v["body"]))
    store.add_documents(docs)
    print(f"[2] 混合索引构建完成（Chroma 向量 + FTS5 BM25），切块数 = {len(docs)}")

    # 3) RAG 检索（llm=None 跳过查询改写，纯离线验证召回链路）
    q = "许成合做过哪些项目？分别用了什么技术栈？"
    cands = retrieve(q, store, None, llm=None)
    print(f"[3] RAG 检索命中 {len(cands)} 条，前 3 条：")
    for c in cands[:3]:
        print("    -", c["metadata"].get("title"), "|", c["text"][:60].replace("\n", " "))

    # 4) 展示将发给 LLM 的 RAG 提示（真实内容已就绪，只差云端调用）
    context = "\n\n".join(
        f"[{c['metadata'].get('title', '')}]\n{c['text']}" for c in cands[:4]
    )
    prompt = (
        "你是简历问答助手，仅根据下方内容回答，不要编造。\n"
        f"上下文：\n{context}\n\n问题：{q}\n回答："
    )
    print("[4] 将发给 LLM 的 RAG 提示（前 280 字）：\n", prompt[:280], "...")

    # 5) 内存测量（Chroma + LangChain + FastAPI 估算；云端 embedding 不占本地内存）
    # 说明：本地沙箱里 psutil 装不上、Windows API 也拿不到真实值，所以这里可能显示 0。
    # 真正观察 Chroma 内存请在 VPS 上用 `docker stats portfolio-app-1`（容器上限 mem_limit=1536m）。
    mb = memory_mb()
    if mb and mb > 1:
        print(f"[5] 进程常驻内存 ≈ {mb:.0f} MB / 2048 MB（2c2g）")
    else:
        print("[5] 进程常驻内存：本地沙箱无法精确测量（无 psutil 且 Windows API 不可用），"
              "请在 VPS 用 `docker stats portfolio-app-1` 观察 Chroma 实际占用（容器上限 mem_limit=1536m）。")

    # 6) 云端密钥探针（沙箱可能无外网，失败属正常，请在 VPS 上验证）
    print("[6] 云端密钥探针（Embedding / LLM）：")
    try:
        emb = get_embeddings()
        v = emb.embed_documents(["探针文本：许成合熟悉 RAG。"])
        print(f"    Embedding OK，向量维度 = {len(v[0])}")
    except Exception as e:
        print("    Embedding 探针失败（沙箱外网受限，属正常）：", str(e)[:140])
    try:
        llm = get_llm()
        r = llm.invoke("用一句话介绍你自己。")
        print("    LLM OK：", r.content[:40])
    except Exception as e:
        print("    LLM 探针失败（沙箱外网受限，属正常）：", str(e)[:140])


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"\n总耗时 {time.time() - t0:.1f}s")
