# ======================================================================
# PDF 摄取服务（管理员上传 → 解析 → 切块 → 实时写入向量库）
#
# 解析优先级（按你��的走云端 API，不引本地重模型）：
#   1) MinerU 云端 API  —— 版式解析强，保留标题/表格结构（需要 MINERU_API_KEY）
#   2) 百度 Paddle 文档解析 API —— OCR 兜底（需要 PADDLE_API_KEY）
#   3) PyMuPDF 本地提取 —— 零额外依赖、最稳；没配密钥时自动走这条（功能可用）
#
# 设计要点：解析与「切块+入库」解耦。parse_pdf 只负责「PDF→文本」，
# 切块/写入由调用方（admin 路由）决定，方便以后接别的来源（网页/Word）。
# ======================================================================
import os
import re
import time

from app.core.config import get_settings
from app.core.logging import get_logger
from app.rag.chunking import chunk_text

log = get_logger("ingest")


def parse_pdf(path: str) -> str:
    """把 PDF 解析成文本/Markdown，按可用方案自动降级。"""
    s = get_settings()
    # 1) MinerU 云端 API（版式解析最强）
    if s.mineru_api_key:
        try:
            return _parse_via_mineru(path, s)
        except Exception as e:
            log.warning("ingest.mineru_failed", error=str(e))
    # 2) 百度 Paddle 文档解析 API（OCR 兜底）
    if s.paddle_api_key:
        try:
            return _parse_via_paddle(path, s)
        except Exception as e:
            log.warning("ingest.paddle_failed", error=str(e))
    # 3) 降级：PyMuPDF 本地提取（一定可用）
    return _parse_via_pymupdf(path)


def _parse_via_pymupdf(path: str) -> str:
    """本地提取：用 PyMuPDF 按页抽文字，拼成纯文本。无需任何密钥。"""
    import fitz  # PyMuPDF
    parts = []
    doc = fitz.open(path)
    for page in doc:
        parts.append(page.get_text())
    doc.close()
    return "\n\n".join(p for p in parts if p.strip())


def _parse_via_mineru(path: str, s) -> str:
    """MinerU 云端 API（mineru.net 官方异步两步式）：签名上传 → 轮询 → 下载 Markdown。

    官方流程：
      1) POST {base}/parse/file  → 拿 task_id + 签名上传 URL（OSS）
      2) PUT 文件到签名 URL
      3) GET  {base}/parse/{task_id} 轮询，state=done 时取 markdown_url
      4) GET  markdown_url 拿到最终 Markdown
    失败（提交失败 / 解析失败 / 超时 / 空结果）都会抛异常，由 parse_pdf 自动降级到 Paddle/PyMuPDF。
    """
    import requests
    base = (s.mineru_api_url or "https://mineru.net/api/v1/agent").rstrip("/")
    auth = {"Authorization": f"Bearer {s.mineru_api_key}"}
    # 1) 提交，拿 task_id + 签名上传 URL
    submit = requests.post(
        f"{base}/parse/file",
        headers={**auth, "Content-Type": "application/json"},
        json={
            "file_name": os.path.basename(path),
            "language": "ch",
            "enable_table": True,
            "is_ocr": False,        # 文本型 PDF 关 OCR 更快；扫描件可在调用处开启
            "enable_formula": True,
        },
        timeout=30,
    )
    submit.raise_for_status()
    sj = submit.json()
    if sj.get("code") != 0:
        raise ValueError(f"MinerU 提交失败: {sj.get('msg')}")
    task_id = sj["data"]["task_id"]
    file_url = sj["data"]["file_url"]
    # 2) 上传文件到签名 URL（OSS，PUT）
    with open(path, "rb") as f:
        up = requests.put(file_url, data=f, timeout=120)
    up.raise_for_status()
    # 3) 轮询任务状态（最长 4 分钟）
    poll_url = f"{base}/parse/{task_id}"
    deadline = time.time() + 240
    md_url = None
    while time.time() < deadline:
        time.sleep(3)
        pr = requests.get(poll_url, headers=auth, timeout=30).json()
        st = (pr.get("data") or {}).get("state")
        if st == "done":
            md_url = (pr.get("data") or {}).get("markdown_url")
            break
        if st == "failed":
            raise ValueError(f"MinerU 解析失败: {(pr.get('data') or {}).get('err_msg')}")
    if not md_url:
        raise TimeoutError("MinerU 解析超时（>240s）")
    # 4) 下载 Markdown
    md = requests.get(md_url, timeout=60).text
    if not md.strip():
        raise ValueError("MinerU 返回的 markdown 为空")
    return md


def _parse_via_paddle(path: str, s) -> str:
    """百度 Paddle / 百度智能云文档解析 API（OCR 兜底）。

    百度文档解析需要先换 access_token，再调解析接口；不同产品端点不同，
    这里实现「通用流程骨架」，具体端点/字段以百度官方为准；失败自动降级。
    """
    import requests
    # 1) 用 API Key/Secret 换 access_token（标准百度鉴权流程）
    token_url = "https://aip.baidubce.com/oauth/2.0/token"
    tok = requests.get(
        token_url,
        params={
            "grant_type": "client_credentials",
            "client_id": s.paddle_api_key,
            "client_secret": s.paddle_api_secret or "",
        },
        timeout=30,
    ).json().get("access_token")
    if not tok:
        raise ValueError("百度 access_token 获取失败")
    # 2) 调文档解析接口（端点以你选用的百度产品为准）
    url = s.paddle_api_url or "https://aip.baidubce.com/rest/2.0/ocr/v1/doc_analysis"
    with open(path, "rb") as f:
        resp = requests.post(
            url,
            params={"access_token": tok},
            files={"pdf_file": f},
            timeout=180,
        )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("text") or (data.get("result") or {}).get("text") or ""
    if not text:
        raise ValueError("百度文档解析返回中没有 text 字段")
    return text


def ingest_pdf_to_store(path: str, source_name: str, store, persist: bool = True) -> tuple[int, str]:
    """解析 PDF → 切块 → 实时写入向量库（Chroma + BM25），返回 (切块数, 解析文本)。

    source_name 用作 doc_id 前缀，方便在向量库里区分「这条来自哪个上传文件」。
    persist=True 时同时把解析文本存进 ingested_docs 表，供「重建索引」时重新切块。
    """
    text = parse_pdf(path)
    s = get_settings()
    docs = chunk_text(
        f"doc::{source_name}",
        source_name,
        text,
        parent_size=s.rag_parent_size,
        child_size=s.rag_child_size,
        child_overlap=s.rag_child_overlap,
    )
    store.add_documents(docs)
    if persist:
        from app.core.db import SessionLocal
        from app.data.models import IngestedDoc
        with SessionLocal() as db:
            db.add(IngestedDoc(source_name=source_name, text=text, chunk_count=len(docs)))
            db.commit()
    return len(docs), text


def ingest_text_to_store(source_name: str, text: str, store, persist: bool = True) -> tuple[int, str]:
    """把一段「已提取的文本」（代码 / Markdown / 文档）切块并实时写入向量库。

    与 ingest_pdf_to_store 的区别：跳过 PDF 解析，直接吃纯文本。
    供「项目代码 / 项目资料」入库用——每个文件作为一个 IngestedDoc 持久化，
    立即可被问答检索；重建索引时也会重新切块（与 PDF 上传走同一张表）。
    """
    s = get_settings()
    docs = chunk_text(
        f"doc::{source_name}",
        source_name,
        text,
        parent_size=s.rag_parent_size,
        child_size=s.rag_child_size,
        child_overlap=s.rag_child_overlap,
    )
    store.add_documents(docs)
    if persist:
        from app.core.db import SessionLocal
        from app.data.models import IngestedDoc
        with SessionLocal() as db:
            db.add(IngestedDoc(source_name=source_name, text=text, chunk_count=len(docs)))
            db.commit()
    return len(docs), text
